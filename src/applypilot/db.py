"""SQLite store (WAL) for boards being polled and the jobs seen on them."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from applypilot.models import ATS, Board, Job

# the poller skips "dead" (404s) and "blocked" (manual removal that re-seeding must not undo)
TIER_RANK = {"dead": -1, "cold": 0, "warm": 1, "hot": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    ats TEXT NOT NULL,
    slug TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'warm',
    source TEXT NOT NULL DEFAULT '',          -- where we learned about it (yc, h1b, manual, ...)
    h1b_approvals INTEGER,
    etag TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0,
    next_poll REAL NOT NULL DEFAULT 0,
    last_polled REAL,
    last_status INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (ats, slug)
);
CREATE INDEX IF NOT EXISTS boards_due ON boards (next_poll) WHERE tier NOT IN ('dead', 'blocked');

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,                      -- ats:slug:ext_id
    ats TEXT NOT NULL,
    slug TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    country TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    posted_at REAL,                           -- per the ATS
    detected_at REAL NOT NULL,                -- when we first saw it
    is_baseline INTEGER NOT NULL DEFAULT 0,   -- seen on a board's first scan (not "new")
    relevant INTEGER,                         -- NULL = not evaluated
    reason TEXT,
    notified_at REAL,
    closed_at REAL,
    source TEXT NOT NULL DEFAULT 'ats',       -- 'ats' (board poll) or the feed that found it first
    raw TEXT
);
CREATE INDEX IF NOT EXISTS jobs_board ON jobs (ats, slug) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS jobs_new ON jobs (detected_at) WHERE relevant = 1;

CREATE TABLE IF NOT EXISTS probes (
    company_key TEXT PRIMARY KEY,             -- normalized company name
    found TEXT NOT NULL,                      -- JSON list of [ats, slug]
    probed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    name TEXT PRIMARY KEY,                    -- github:SimplifyJobs/New-Grad-Positions, hn, ...
    etag TEXT,
    cursor TEXT,                              -- feed-specific position (HN: thread id + last comment time)
    baseline_done INTEGER NOT NULL DEFAULT 0,
    next_poll REAL NOT NULL DEFAULT 0,
    last_polled REAL,
    last_status INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feed_items (
    feed TEXT NOT NULL,
    item_id TEXT NOT NULL,                    -- the feed's own id (listing id, HN comment id)
    job_id TEXT,                              -- jobs.id it resolved to, if it named one job
    seen_at REAL NOT NULL,
    PRIMARY KEY (feed, item_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,                -- normalized company name, for the per-company cap
    title TEXT NOT NULL,
    ats TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,                     -- submitted | unconfirmed | abandoned | failed
    resume TEXT,
    answers TEXT,                             -- JSON: every field, what was filled, its source and readback
    completed_by_user TEXT,                   -- JSON list of the questions the user answered in the window
    started_at REAL NOT NULL,
    submitted_at REAL,
    confirmation TEXT,                        -- the confirmation page's URL / text that proved submission
    screenshot_filled TEXT,
    screenshot_after TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS applications_company ON applications (company_key, submitted_at);
CREATE INDEX IF NOT EXISTS applications_job ON applications (job_id);
"""

# columns added after a table first shipped; connect() adds any that are missing
MIGRATIONS = {
    "jobs": {
        "source": "TEXT NOT NULL DEFAULT 'ats'",
        # description screening (rank.screen): priority 0-100, visa signals, soft knockouts
        "score": "REAL",
        "sponsorship": "TEXT",  # offers | no | unknown
        "everify": "INTEGER",
        "flags": "TEXT",  # JSON list
        "screened_at": "REAL",
        # resume fit (fit.py): best pre-built variant and its ATS score against this JD
        "fit": "REAL",
        "variant": "TEXT",
        # a resume tailored to this job (tailor.py)
        "tailored_pdf": "TEXT",
        "tailored_score": "REAL",
    },
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN where needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return conn


def add_board(
    conn: sqlite3.Connection,
    ats: ATS | str,
    slug: str,
    company: str = "",
    tier: str = "warm",
    source: str = "manual",
    h1b_approvals: int | None = None,
) -> bool:
    """Insert a board, or upgrade an existing one's tier/company/H-1B count. Returns True if new."""
    row = conn.execute(
        "SELECT tier, company, baseline_done FROM boards WHERE ats=? AND slug=?", (str(ats), slug)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO boards (ats, slug, company, tier, source, h1b_approvals) VALUES (?,?,?,?,?,?)",
            (str(ats), slug, company, tier, source, h1b_approvals),
        )
        return True
    if row["tier"] == "blocked":
        return False
    # cold after a baseline = scanned and found no US jobs; re-seeding must not undo that (only the
    # poller's own promotion on a new relevant job does)
    sticky = row["tier"] == "cold" and row["baseline_done"]
    new_tier = tier if TIER_RANK[tier] > TIER_RANK[row["tier"]] and not sticky else row["tier"]
    conn.execute(
        "UPDATE boards SET tier=?, company=COALESCE(NULLIF(company,''), ?),"
        " h1b_approvals=COALESCE(?, h1b_approvals) WHERE ats=? AND slug=?",
        (new_tier, company, h1b_approvals, str(ats), slug),
    )
    return False


def due_boards(conn: sqlite3.Connection, now: float, limit: int = 500) -> list[Board]:
    rows = conn.execute(
        "SELECT * FROM boards WHERE tier NOT IN ('dead', 'blocked') AND next_poll <= ? ORDER BY next_poll LIMIT ?",
        (now, limit),
    ).fetchall()
    return [
        Board(ATS(r["ats"]), r["slug"], r["company"], r["tier"], r["etag"], bool(r["baseline_done"]))
        for r in rows
    ]


def next_due(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MIN(next_poll) FROM boards WHERE tier NOT IN ('dead', 'blocked')").fetchone()
    return row[0]


def record_poll(
    conn: sqlite3.Connection, board: Board, status: int, etag: str | None, now: float, next_poll: float
) -> None:
    ok = status in (200, 304)
    conn.execute(
        "UPDATE boards SET etag=?, last_polled=?, last_status=?, next_poll=?,"
        " baseline_done = CASE WHEN ? THEN 1 ELSE baseline_done END,"
        " fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END,"
        # a board that 404s three times in a row has moved ATS or closed
        " tier = CASE WHEN ? AND fail_count + 1 >= 3 THEN 'dead' ELSE tier END"
        " WHERE ats=? AND slug=?",
        (etag, now, status, next_poll, status == 200, ok, status == 404, str(board.ats), board.slug),
    )


def set_tier(conn: sqlite3.Connection, ats: ATS | str, slug: str, tier: str) -> None:
    conn.execute("UPDATE boards SET tier=? WHERE ats=? AND slug=?", (tier, str(ats), slug))


def board_ids(conn: sqlite3.Connection, ats: ATS | str, slug: str) -> dict[str, bool]:
    """Every job id ever seen on a board -> whether it is still open."""
    rows = conn.execute(
        "SELECT ext_id, closed_at IS NULL FROM jobs WHERE ats=? AND slug=?", (str(ats), slug)
    ).fetchall()
    return {r[0]: bool(r[1]) for r in rows}


def reopen_jobs(conn: sqlite3.Connection, ats: ATS | str, slug: str, ext_ids: Iterable[str]) -> None:
    conn.executemany(
        "UPDATE jobs SET closed_at=NULL WHERE ats=? AND slug=? AND ext_id=?",
        [(str(ats), slug, e) for e in ext_ids],
    )


def insert_jobs(conn: sqlite3.Connection, jobs: Iterable[Job], detected_at: float, baseline: bool) -> None:
    conn.executemany(
        "INSERT INTO jobs (id, ats, slug, ext_id, company, title, location, country, remote, url,"
        " apply_url, description, posted_at, detected_at, is_baseline, source, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        [
            (
                j.id, str(j.ats), j.slug, j.ext_id, j.company, j.title, j.location, j.country,
                int(j.remote), j.url, j.apply_url, j.description, j.posted_at, detected_at,
                int(baseline), j.source, json.dumps(j.raw, default=str),
            )
            for j in jobs
        ],
    )


def close_jobs(conn: sqlite3.Connection, ats: ATS | str, slug: str, ext_ids: Iterable[str], now: float) -> None:
    conn.executemany(
        "UPDATE jobs SET closed_at=? WHERE ats=? AND slug=? AND ext_id=?",
        [(now, str(ats), slug, e) for e in ext_ids],
    )


def set_description(conn: sqlite3.Connection, job_id: str, description: str) -> None:
    conn.execute("UPDATE jobs SET description=? WHERE id=?", (description, job_id))


def set_screen(conn: sqlite3.Connection, job_id: str, score: float, sponsorship: str, everify: bool, flags: str,
               now: float) -> None:
    conn.execute("UPDATE jobs SET score=?, sponsorship=?, everify=?, flags=?, screened_at=? WHERE id=?",
                 (score, sponsorship, int(everify), flags, now, job_id))


def set_fit(conn: sqlite3.Connection, job_id: str, fit: float | None, variant: str | None) -> None:
    conn.execute("UPDATE jobs SET fit=?, variant=? WHERE id=?", (fit, variant, job_id))


def set_tailored(conn: sqlite3.Connection, job_id: str, pdf: str, score: float | None) -> None:
    conn.execute("UPDATE jobs SET tailored_pdf=?, tailored_score=? WHERE id=?", (pdf, score, job_id))


def set_relevance(conn: sqlite3.Connection, job_id: str, relevant: bool, reason: str) -> None:
    conn.execute("UPDATE jobs SET relevant=?, reason=? WHERE id=?", (int(relevant), reason, job_id))


def get_probe(
    conn: sqlite3.Connection, key: str, negative_ttl_s: float | None = None, now: float | None = None
) -> list[list[str]] | None:
    """Cached probe result, or None (probe again). A "no board found" result expires after
    `negative_ttl_s`: companies move ATS, and a company that just posted a job is worth a recheck."""
    row = conn.execute("SELECT found, probed_at FROM probes WHERE company_key=?", (key,)).fetchone()
    if row is None:
        return None
    found = json.loads(row[0])
    if not found and negative_ttl_s is not None and (now or time.time()) - row[1] > negative_ttl_s:
        return None
    return found


def save_probe(conn: sqlite3.Connection, key: str, found: list[tuple[str, str]], now: float) -> None:
    conn.execute(
        "INSERT INTO probes (company_key, found, probed_at) VALUES (?,?,?)"
        " ON CONFLICT(company_key) DO UPDATE SET found=excluded.found, probed_at=excluded.probed_at",
        (key, json.dumps(found), now),
    )


def mark_notified(conn: sqlite3.Connection, job_ids: Iterable[str], now: float) -> None:
    conn.executemany("UPDATE jobs SET notified_at=? WHERE id=?", [(now, i) for i in job_ids])


def add_application(conn: sqlite3.Connection, **row) -> int:
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO applications ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    return cur.lastrowid


def update_application(conn: sqlite3.Connection, app_id: int, **row) -> None:
    sets = ", ".join(f"{k}=?" for k in row)
    conn.execute(f"UPDATE applications SET {sets} WHERE id=?", [*row.values(), app_id])


def applied_to(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """The submitted (or unconfirmed) application for this job, if any."""
    return conn.execute("SELECT * FROM applications WHERE job_id=? AND status IN ('submitted', 'unconfirmed')"
                        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def company_applications(conn: sqlite3.Connection, company_key: str, since: float) -> int:
    return conn.execute("SELECT COUNT(*) FROM applications WHERE company_key=? AND status IN ('submitted',"
                        " 'unconfirmed') AND COALESCE(submitted_at, started_at) >= ?",
                        (company_key, since)).fetchone()[0]


def row_job(r: sqlite3.Row) -> Job:
    return Job(r["ats"], r["slug"], r["ext_id"], r["title"], r["company"], r["location"], r["country"],
               bool(r["remote"]), r["url"], r["apply_url"], r["description"], r["posted_at"], r["source"])


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None


# feeds: sources that list jobs or companies across many employers (GitHub lists, HN, alert emails)


def feed_state(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    conn.execute("INSERT INTO feeds (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return conn.execute("SELECT * FROM feeds WHERE name=?", (name,)).fetchone()


def record_feed(
    conn: sqlite3.Connection, name: str, status: int, etag: str | None, cursor: str | None,
    now: float, next_poll: float,
) -> None:
    ok = status in (200, 304)
    conn.execute(
        "UPDATE feeds SET etag=?, cursor=COALESCE(?, cursor), last_polled=?, last_status=?, next_poll=?,"
        " baseline_done = CASE WHEN ? THEN 1 ELSE baseline_done END,"
        " fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END WHERE name=?",
        (etag, cursor, now, status, next_poll, status == 200, ok, name),
    )


def unseen_items(conn: sqlite3.Connection, feed: str, item_ids: Iterable[str]) -> set[str]:
    seen = {r[0] for r in conn.execute("SELECT item_id FROM feed_items WHERE feed=?", (feed,))}
    return set(item_ids) - seen


def mark_items(conn: sqlite3.Connection, feed: str, items: Iterable[tuple[str, str | None]], now: float) -> None:
    conn.executemany(
        "INSERT INTO feed_items (feed, item_id, job_id, seen_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
        [(feed, i, j, now) for i, j in items],
    )
