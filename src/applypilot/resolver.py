"""Turn any job link (new-grad lists, HN posts, alert emails) into the employer's ATS job.

`parse` is pure pattern matching on known ATS URL shapes. `resolve` adds the network steps: it
follows short links (grnh.se, lnkd.in, bit.ly), recovers a Greenhouse board slug from a bare
`gh_jid` through the embed redirect, and registers every pollable board it finds, so the company's
next posting is caught at the source. That is how the board list grows by itself.

A resolved job gets the same id the board poller would give it (`ats:slug:ext_id`), so a job seen
on a feed and on its ATS board is one row and one push.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

import httpx

from applypilot import db
from applypilot.models import ATS, Board
from applypilot.seeds import Seed, add_companies, company_key

POLLABLE = {a.value for a in ATS}
SHORTENERS = {"grnh.se", "lnkd.in", "bit.ly", "t.co", "tinyurl.com", "buff.ly", "ow.ly", "rebrand.ly", "shorturl.at"}
# query params that only track the click; dropping them makes the same job's links compare equal
_TRACKING = re.compile(
    r"^(utm_\w+|ref|refid|gh_src|src|source|lever-source|lever-origin|trk\w*|trackingid|ccuid)$", re.I
)
# job boards that re-list employer postings: their links are a last resort (we never fetch them),
# the employer's own posting is found by company + title instead
AGGREGATORS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "dice.com", "monster.com",
    "simplyhired.com", "careerbuilder.com", "talent.com", "jooble.org", "adzuna.com", "lensa.com",
    "jobright.ai", "builtin.com", "wellfound.com", "joinhandshake.com", "theladders.com", "snagajob.com",
    "bebee.com", "learn4good.com", "jobleads.com", "salary.com", "recruit.net", "jobilize.com",
    "careerjet.com", "getwork.com", "tallo.com", "whatjobs.com", "teal.com", "jobs.weekday.works",
    # seen as JSearch publishers (2026-10-20)
    "jobgether.com", "jobmesh.io", "remote.co", "jobtoday.com", "clearancejobs.com", "himalayas.app",
    "remoterocketship.com", "workingnomads.com", "hiring.cafe", "flexjobs.com", "virtualvocations.com",
    "jobsora.com", "jobrapido.com", "trabajo.org", "jobs.institutedata.com", "localjobs.com",
}
_WORKDAY = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass
class Link:
    ats: str  # greenhouse | lever | ashby | workday | smartrecruiters | workable | other
    slug: str  # board slug; Workday "tenant.wdN/site"; other: the host. "" = not known yet
    ext_id: str | None  # the job's id on that ATS; None for a board-level link
    url: str  # canonical URL, tracking params removed
    new_board: bool = False  # resolve() registered this board for polling

    @property
    def pollable(self) -> bool:
        return self.ats in POLLABLE and bool(self.slug)

    @property
    def job_id(self) -> str | None:
        return f"{self.ats}:{self.slug}:{self.ext_id}" if self.slug and self.ext_id else None

    @property
    def apply_url(self) -> str:
        if not (self.slug and self.ext_id):
            return self.url
        return {
            "greenhouse": f"https://job-boards.greenhouse.io/{self.slug}/jobs/{self.ext_id}",
            "lever": f"https://jobs.lever.co/{self.slug}/{self.ext_id}/apply",
            "ashby": f"https://jobs.ashbyhq.com/{self.slug}/{self.ext_id}/application",
        }.get(self.ats, self.url)


def is_aggregator(url: str) -> bool:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in AGGREGATORS)


def best_url(urls: list[str]) -> str | None:
    """The most direct link among a job's apply options: a pollable ATS, then any other ATS, then the
    employer's own page. None if every link is an aggregator."""
    links = [(u, parse(u)) for u in urls if u and not is_aggregator(u)]
    links = [(u, link) for u, link in links if link]
    for want in (lambda lk: lk.ats in POLLABLE, lambda lk: lk.ats != "other", lambda lk: True):
        for u, link in links:
            if want(link):
                return u
    return None


def clean_url(url: str) -> str:
    u = urlsplit(url.strip())
    params = parse_qs(u.query, keep_blank_values=True)
    # first value only: some career sites repeat a param (?gh_jid=1&gh_jid=1)
    q = [(k, vs[0]) for k, vs in params.items() if not _TRACKING.match(k)]
    return urlunsplit((u.scheme or "https", u.netloc.lower(), u.path, urlencode(q), ""))


def _other(url: str, host: str) -> Link:
    return Link("other", host, hashlib.sha1(url.encode()).hexdigest()[:16], url)


def parse(url: str) -> Link | None:
    """Recognize a job (or board) URL without any network call. None if it isn't a web URL."""
    if not re.match(r"^https?://", url.strip(), re.I):
        return None
    clean = clean_url(url)
    u = urlsplit(clean)
    host = u.netloc.removeprefix("www.")
    parts = [unquote(p) for p in u.path.split("/") if p]
    q = {k: v[0] for k, v in parse_qs(u.query).items()}

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        if parts[:1] == ["embed"]:
            return Link("greenhouse", q.get("for", "").lower(), q.get("token"), clean)
        if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
            return Link("greenhouse", parts[0].lower(), parts[2], clean)
        if len(parts) == 1:
            return Link("greenhouse", parts[0].lower(), q.get("gh_jid"), clean)
    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
        jid = parts[4] if len(parts) >= 5 and parts[3] == "jobs" else None
        return Link("greenhouse", parts[2].lower(), jid, clean)
    # Lever and Ashby job ids are UUIDs; anything else after the slug is a board page
    uuid = parts[1].lower() if len(parts) >= 2 and _UUID.match(parts[1]) else None
    if host == "jobs.lever.co" and parts:
        return Link("lever", parts[0], uuid, clean)  # Lever slugs are case-sensitive
    if host == "jobs.ashbyhq.com" and parts:
        return Link("ashby", parts[0].lower(), uuid, clean)
    if m := _WORKDAY.match(host):
        parts = [p for p in parts if not _LOCALE.match(p)]
        if parts:
            ext = parts[-1].rsplit("_", 1)[-1] if "job" in parts[1:] else None
            return Link("workday", f"{m[1]}.{m[2]}/{parts[0]}", ext, clean)
    if host == "jobs.smartrecruiters.com" and len(parts) >= 2 and (m := re.match(r"\d+", parts[1])):
        return Link("smartrecruiters", parts[0].lower(), m[0], clean)
    if host == "apply.workable.com" and parts:
        return Link("workable", parts[0].lower(), parts[2] if len(parts) >= 3 and parts[1] == "j" else None, clean)
    # employer career pages that embed an ATS: the job id is in the query, the board slug is not
    if q.get("gh_jid", "").isdigit():
        return Link("greenhouse", "", q["gh_jid"], clean)
    if "ashby_jid" in q:
        return Link("ashby", "", q["ashby_jid"], clean)
    return _other(clean, host)


async def _expand(client: httpx.AsyncClient, url: str, hops: int = 3) -> str:
    """Follow short-link redirects one hop at a time (stopping at the first non-shortener)."""
    for _ in range(hops):
        if urlsplit(url).netloc.lower().removeprefix("www.") not in SHORTENERS:
            break
        r = await client.get(url, follow_redirects=False)
        loc = r.headers.get("location")
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            break
        url = str(httpx.URL(url).join(loc))
    return url


async def _greenhouse_slug(client: httpx.AsyncClient, jid: str) -> str:
    """boards.greenhouse.io/embed/job_app?token=JID redirects to ...?for=SLUG&token=JID (404 if unknown)."""
    r = await client.get(f"https://boards.greenhouse.io/embed/job_app?token={jid}", follow_redirects=False)
    loc = r.headers.get("location", "")
    return (parse_qs(urlsplit(loc).query).get("for") or [""])[0].lower()


def boards_for_company(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    """Live boards whose company name normalizes to the same key as `company`."""
    key = company_key(company)
    if not key:
        return []
    rows = conn.execute("SELECT * FROM boards WHERE tier NOT IN ('dead', 'blocked')").fetchall()
    return [r for r in rows if company_key(r["company"] or r["slug"]) == key]


async def resolve(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    url: str,
    company: str = "",
    source: str = "",
    tier: str = "hot",
) -> Link | None:
    """parse() plus the network steps; registers the board when the link names a pollable one.

    A link on a pollable ATS whose slug can't be recovered degrades to "other", so every returned
    job link has a slug and an ext_id."""
    link = parse(url)
    if link is None:
        return None
    if link.ats == "other" and link.slug in SHORTENERS:
        try:
            link = parse(await _expand(client, link.url)) or link
        except httpx.HTTPError:
            return link
    if link.ats in POLLABLE and not link.slug:
        try:
            if link.ats == "greenhouse" and link.ext_id:
                link.slug = await _greenhouse_slug(client, link.ext_id)
        except httpx.HTTPError:
            pass
        if not link.slug and company:
            # e.g. an Ashby job embedded on the employer's site: use the company's known board
            link.slug = next((b["slug"] for b in boards_for_company(conn, company) if b["ats"] == link.ats), "")
        if not link.slug:
            return _other(link.url, urlsplit(link.url).netloc.removeprefix("www."))
    if link.pollable:
        link.new_board = db.add_board(conn, link.ats, link.slug, company or link.slug, tier, source)
    return link


# --- resolving by company + title (alert emails name the job but link to LinkedIn/Indeed) ---

_WORDS = re.compile(r"[a-z0-9+#]+")


def _norm_title(t: str) -> str:
    return " ".join(_WORDS.findall(t.lower().replace("&", " and ")))


def title_score(a: str, b: str) -> float:
    """0..1 similarity of two job titles: max of character ratio and word overlap."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    wa, wb = set(na.split()), set(nb.split())
    return max(difflib.SequenceMatcher(None, na, nb).ratio(), len(wa & wb) / len(wa | wb))


def find_on_boards(
    conn: sqlite3.Connection, company: str, title: str, location: str = "", min_score: float = 0.85
) -> sqlite3.Row | None:
    """The open job on one of the company's tracked boards that best matches the title."""
    boards = boards_for_company(conn, company)
    best, best_key = None, (min_score, 0.0, 0.0)
    for b in boards:
        for r in conn.execute(
            "SELECT * FROM jobs WHERE ats=? AND slug=? AND closed_at IS NULL", (b["ats"], b["slug"])
        ):
            score = title_score(title, r["title"])
            # ties (same title in several cities) go to the matching location, then the newest posting
            loc = 1.0 if location and location.split(",")[0].lower() in (r["location"] or "").lower() else 0.0
            key = (score, loc, r["posted_at"] or 0.0)
            if key >= best_key:
                best, best_key = r, key
    return best


async def resolve_by_name(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    poller,
    company: str,
    title: str,
    location: str = "",
    source: str = "",
) -> sqlite3.Row | None:
    """Find the ATS job for (company, title): tracked boards first; otherwise probe the company's
    ATS boards, scan the new ones once (baseline, no pushes), and look again."""
    if hit := find_on_boards(conn, company, title, location):
        return hit
    # the company is hiring right now, so an old "no board" result is worth one more look
    await add_companies(conn, client, [Seed(company, None, "hot", source)], negative_ttl_s=86400)
    for r in boards_for_company(conn, company):
        if not r["baseline_done"]:
            await poller.poll_board(Board(ATS(r["ats"]), r["slug"], r["company"], r["tier"], r["etag"], False))
    return find_on_boards(conn, company, title, location)
