"""Read queries behind the dashboard pages (one SQLite connection per request)."""

from __future__ import annotations

import json
import statistics
import time

APPLIED = "SELECT job_id FROM applications WHERE status IN ('submitted','unconfirmed')"
ATS_FILLABLE = ("greenhouse", "lever", "ashby")


def readiness(prep) -> dict:
    """What the queue shows for a job: not prepared, unreadable, ready, or N for you (+ drafts)."""
    if prep is None:
        return {"label": "not prepared", "kind": "muted"}
    if prep["error"]:
        return {"label": "form unreadable", "kind": "bad"}
    n = prep["needs_count"] or 0
    k = len(json.loads(prep["drafts"] or "[]"))
    label = ("ready" if n == 0 else f"{n} for you") + (f" · {k} drafted" if k else "")
    return {"label": label, "kind": "good" if n == 0 else "warn"}


def home(conn, now: float | None = None) -> dict:
    now = now or time.time()
    day = now - 86400
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    lags = [r[0] / 60 for r in conn.execute(
        "SELECT detected_at - posted_at FROM jobs WHERE is_baseline=0 AND relevant=1 AND source='ats'"
        " AND posted_at IS NOT NULL AND detected_at >= ? AND detected_at - posted_at <= 172800", (now - 7 * 86400,))]
    return {
        "new_jobs": one("SELECT COUNT(*) FROM jobs WHERE relevant=1 AND is_baseline=0 AND detected_at >= ?", day),
        "dropped": one("SELECT COUNT(*) FROM jobs WHERE reason LIKE 'hard drop:%' AND detected_at >= ?", day),
        "applied": one("SELECT COUNT(*) FROM applications WHERE status='submitted' AND submitted_at >= ?", day),
        "applied_total": one("SELECT COUNT(*) FROM applications WHERE status='submitted'"),
        "interviews": one("SELECT COUNT(*) FROM applications WHERE outcome='interview'"),
        "responses": conn.execute(
            "SELECT r.*, a.company, a.title FROM responses r JOIN applications a ON a.id=r.application_id"
            " WHERE r.kind != 'other' ORDER BY r.received_at DESC LIMIT 6").fetchall(),
        "latency_p50": round(statistics.median(lags), 1) if lags else None,
        "latency_n": len(lags),
        "queue": queue(conn, limit=8),
    }


def queue(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        f"SELECT j.*, p.error AS p_error, p.needs_count, p.drafts AS p_drafts, p.job_id AS p_id FROM jobs j"
        f" LEFT JOIN prepared p ON p.job_id=j.id WHERE j.relevant=1 AND j.closed_at IS NULL"
        f" AND COALESCE(j.dismissed,0)=0 AND j.ats IN {ATS_FILLABLE} AND j.score IS NOT NULL"
        f" AND j.posted_at >= ? AND j.id NOT IN ({APPLIED}) ORDER BY j.score DESC LIMIT ?",
        (time.time() - 14 * 86400, limit)).fetchall()
    out = []
    for r in rows:
        prep = None if r["p_id"] is None else {"error": r["p_error"], "needs_count": r["needs_count"],
                                                 "drafts": r["p_drafts"]}
        out.append({**dict(r), "ready": readiness(prep)})
    return out


SORTS = {"score": "j.score DESC NULLS LAST, j.posted_at DESC", "posted": "j.posted_at DESC NULLS LAST",
         "fit": "j.fit DESC NULLS LAST, j.score DESC"}


def jobs(conn, *, q: str = "", min_score: float = 0, days: float = 14, ats: str = "", visa: str = "",
         hide_applied: bool = True, sort: str = "score", page: int = 1, per_page: int = 50) -> tuple[list, int]:
    where = ["j.relevant=1", "j.closed_at IS NULL", "COALESCE(j.dismissed,0)=0", "COALESCE(j.score,0) >= ?",
             "COALESCE(j.posted_at, j.detected_at) >= ?"]
    args: list = [min_score, time.time() - days * 86400]
    if q:
        where.append("(j.company LIKE ? OR j.title LIKE ? OR j.location LIKE ?)")
        args += [f"%{q}%"] * 3
    if ats == "fillable":
        where.append(f"j.ats IN {ATS_FILLABLE}")
    elif ats:
        where.append("j.ats = ?")
        args.append(ats)
    if visa == "sponsors":
        where.append("j.sponsorship = 'offers'")
    elif visa == "opt":
        where.append("j.sponsorship = 'no'")
    elif visa == "everify":
        where.append("j.everify = 1")
    if hide_applied:
        where.append(f"j.id NOT IN ({APPLIED})")
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {sql_where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT j.*, p.error AS p_error, p.needs_count, p.drafts AS p_drafts, p.job_id AS p_id,"
        f" (j.id IN ({APPLIED})) AS applied FROM jobs j LEFT JOIN prepared p ON p.job_id=j.id"
        f" WHERE {sql_where} ORDER BY {SORTS.get(sort, SORTS['score'])} LIMIT ? OFFSET ?",
        [*args, per_page, (max(1, page) - 1) * per_page]).fetchall()
    out = []
    for r in rows:
        prep = None if r["p_id"] is None else {"error": r["p_error"], "needs_count": r["needs_count"],
                                                 "drafts": r["p_drafts"]}
        out.append({**dict(r), "ready": readiness(prep) if r["ats"] in ATS_FILLABLE else
                    {"label": "apply on site", "kind": "muted"}})
    return out, total


def applications(conn, status: str = "") -> list:
    where, args = ("WHERE status = ?", [status]) if status else ("WHERE status != 'started'", [])
    return conn.execute(f"SELECT * FROM applications {where} ORDER BY COALESCE(submitted_at, started_at) DESC"
                        f" LIMIT 300", args).fetchall()


def application(conn, app_id: int) -> dict | None:
    a = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if a is None:
        return None
    answers = json.loads(a["answers"] or "{}")
    filled = answers.get("filled", answers if isinstance(answers, list) else [])
    return {"app": a, "filled": filled, "completed": json.loads(a["completed_by_user"] or "[]"),
            "responses": conn.execute("SELECT * FROM responses WHERE application_id=? ORDER BY received_at",
                                      (app_id,)).fetchall()}
