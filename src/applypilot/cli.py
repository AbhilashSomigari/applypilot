"""`applypilot` command line."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import statistics
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from applypilot import db, notify, resolver, seeds
from applypilot.config import get_settings, home, write_default_config
from applypilot.feeds import FeedRunner, FeedStats
from applypilot.models import ATS, Job
from applypilot.poller import Poller, PollStats, make_client

app = typer.Typer(no_args_is_help=True, help="Catch new postings on company ATS boards within minutes.")
boards_app = typer.Typer(no_args_is_help=True, help="Manage the boards being polled.")
seed_app = typer.Typer(no_args_is_help=True, help="Discover boards from company lists.")
app.add_typer(boards_app, name="boards")
feeds_app = typer.Typer(no_args_is_help=True, help="Cross-employer feeds: GitHub new-grad lists, HN.")
app.add_typer(seed_app, name="seed")
app.add_typer(feeds_app, name="feeds")
gmail_app = typer.Typer(no_args_is_help=True, help="Read-only Gmail access for job-alert emails.")
app.add_typer(gmail_app, name="gmail")
console = Console()
REPO = Path(__file__).resolve().parents[2]


def _conn():
    return db.connect(get_settings().db_path)


def _setup_logging() -> None:
    home().mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(home() / "poller.log", maxBytes=5_000_000, backupCount=3)
    logging.basicConfig(
        handlers=[handler],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request at INFO (~300K lines/day at full polling); keep only its warnings
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def init() -> None:
    """Create ~/.applypilot (config, database, profile and answers templates)."""
    cfg = write_default_config()
    _conn()
    for name in ("profile", "answers"):
        dst = home() / f"{name}.yaml"
        if not dst.exists():
            shutil.copy(REPO / "profile" / f"{name}.example.yaml", dst)
    s = get_settings()
    console.print(f"[green]ready[/] {home()}\n  config: {cfg}\n  db: {s.db_path}")
    console.print(f"  phone alerts: install the ntfy app and subscribe to topic [bold]{s.ntfy_topic}[/]")


@boards_app.command("add")
def boards_add(ats: ATS, slug: str, company: str = "", tier: str = "hot") -> None:
    new = db.add_board(_conn(), ats, slug, company or slug, tier, "manual")
    console.print("added" if new else "already tracked (tier upgraded if higher)")


@boards_app.command("block")
def boards_block(ats: ATS, slug: str) -> None:
    """Stop polling a board for good (e.g. a different company that shares the slug)."""
    conn = _conn()
    db.set_tier(conn, ats, slug, "blocked")
    conn.execute("DELETE FROM jobs WHERE ats=? AND slug=?", (str(ats), slug))
    console.print(f"blocked {ats}/{slug}")


@boards_app.command("list")
def boards_list(tier: str | None = None, limit: int = 50) -> None:
    conn = _conn()
    q = "SELECT ats, slug, company, tier, source, h1b_approvals, last_status, fail_count FROM boards"
    args: tuple = ()
    if tier:
        q, args = q + " WHERE tier=?", (tier,)
    rows = conn.execute(q + " ORDER BY tier DESC, company LIMIT ?", (*args, limit)).fetchall()
    t = Table("ats", "slug", "company", "tier", "source", "h1b", "last", "fails")
    for r in rows:
        t.add_row(*(str(x) if x is not None else "" for x in r))
    console.print(t)


def _seed(seed_list: list[seeds.Seed]) -> None:
    conn = _conn()
    s = get_settings()

    async def go():
        async with make_client(s) as client, asyncio.timeout(3600):
            with Progress(console=console) as prog:
                task = prog.add_task("probing ATS boards", total=len(seed_list))
                return await seeds.add_companies(
                    conn, client, seed_list, on_progress=lambda: prog.advance(task)
                )

    counts = asyncio.run(go())
    console.print(dict(counts))


@seed_app.command("curated")
def seed_curated(path: Path = REPO / "seeds" / "companies.txt", tier: str = "hot") -> None:
    """Probe the curated company list (hot tier by default)."""
    _seed(seeds.file_seeds(path, tier))


@seed_app.command("yc")
def seed_yc(file: Path | None = None) -> None:
    """Probe every actively hiring YC company with US/remote presence (yc-oss dataset)."""
    data = json.loads(file.read_text()) if file else httpx.get(seeds.YC_URL, timeout=60).json()
    _seed(seeds.yc_seeds(data))


@seed_app.command("h1b")
def seed_h1b(file: Path | None = None, min_approvals: int = 25) -> None:
    """Probe employers with >= N approvals in the USCIS H-1B Employer Data Hub export."""
    if file is None:
        file = home() / "h1b_datahub.csv"
        if not file.exists():
            with httpx.stream("GET", seeds.H1B_URL, timeout=120, follow_redirects=True) as r, open(file, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    _seed(seeds.h1b_seeds(file, min_approvals, get_settings().hot_h1b_threshold))


def _print_cycle(stats: PollStats) -> None:
    for job, reason in stats.relevant:
        console.print(
            f"[bold green]NEW[/] {job.company}: {job.title} · {job.location} · "
            f"{notify.age(job.posted_at)} · {reason}\n    {job.apply_url or job.url}"
        )
    if stats.fresh_baseline:
        console.print(f"[cyan]{len(stats.fresh_baseline)} relevant jobs posted in the last "
                      f"{get_settings().fresh_hours} h found on first scans[/] (see `applypilot jobs --fresh`)")
    console.print(
        f"[dim]{time.strftime('%H:%M:%S')} polled {stats.polled} · 304 {stats.unchanged} · "
        f"errors {stats.errors} · new {stats.new_jobs} · relevant {len(stats.relevant)}[/]"
    )


@app.command()
def poll(push: bool = True) -> None:
    """Poll every due board once."""
    _setup_logging()
    s = get_settings()

    async def go():
        async with make_client(s) as client:
            return await Poller(_conn(), s, client, push=push).run_once()

    _print_cycle(asyncio.run(go()))


class _Quiet:
    """Collects cycle stats and prints them only when something happened or every `every_s`,
    so run.log doesn't get a line per second of all-304 cycles."""

    def __init__(self, printer, every_s: float = 300) -> None:
        self.printer, self.every_s = printer, every_s
        self.pending = None
        self.last = 0.0

    def __call__(self, stats) -> None:
        if self.pending is None:
            self.pending = stats
        else:
            self.pending.merge(stats)
        p = self.pending
        if p.relevant or p.fresh_baseline or p.errors or time.time() - self.last >= self.every_s:
            self.printer(p)
            self.pending, self.last = None, time.time()


def _print_feed_cycle(stats: FeedStats) -> None:
    for job, reason in stats.relevant:
        console.print(
            f"[bold green]NEW[/] {job.company}: {job.title} · {job.location} · "
            f"{notify.age(job.posted_at)} · {reason}\n    {job.apply_url or job.url}"
        )
    if stats.fresh_baseline:
        console.print(f"[cyan]{len(stats.fresh_baseline)} relevant jobs posted in the last "
                      f"{get_settings().fresh_hours} h found on feed baselines[/] (see `applypilot jobs --fresh`)")
    console.print(
        f"[dim]{time.strftime('%H:%M:%S')} feeds {stats.feeds} · 304 {stats.unchanged} · errors {stats.errors} · "
        f"leads {stats.leads} · new jobs {stats.new_jobs} (already known {stats.known_jobs}) · "
        f"new boards {stats.new_boards} · companies probed {stats.probed} · relevant {len(stats.relevant)}[/]"
    )


@app.command()
def run(push: bool = True, feeds: bool = True) -> None:
    """Poll boards and feeds forever (what the launchd agent runs)."""
    _setup_logging()
    s = get_settings()

    async def go():
        conn = _conn()
        async with make_client(s) as client:
            loops = [Poller(conn, s, client, push=push).run_forever(on_cycle=_Quiet(_print_cycle))]
            if feeds:
                loops.append(FeedRunner(conn, s, client, push=push).run_forever(on_cycle=_Quiet(_print_feed_cycle)))
            await asyncio.gather(*loops)

    asyncio.run(go())


@feeds_app.command("poll")
def feeds_poll(push: bool = True, force: bool = False) -> None:
    """Fetch every due feed once (--force: all feeds, due or not)."""
    _setup_logging()
    s = get_settings()

    async def go():
        async with make_client(s) as client:
            return await FeedRunner(_conn(), s, client, push=push).run_once(force=force)

    _print_feed_cycle(asyncio.run(go()))


@feeds_app.command("list")
def feeds_list() -> None:
    conn = _conn()
    t = Table("feed", "baseline", "last poll", "status", "fails", "items", "jobs")
    for r in conn.execute("SELECT * FROM feeds ORDER BY name").fetchall():
        items, jobs = conn.execute(
            "SELECT COUNT(*), COUNT(job_id) FROM feed_items WHERE feed=?", (r["name"],)).fetchone()
        last = time.strftime("%m-%d %H:%M", time.localtime(r["last_polled"])) if r["last_polled"] else ""
        t.add_row(r["name"], "done" if r["baseline_done"] else "pending", last, str(r["last_status"] or ""),
                  str(r["fail_count"]), str(items), str(jobs))
    console.print(t)
    js = get_settings().feeds.jsearch
    if js.api_key:
        console.print(f"Google Jobs (JSearch): {len(js.queries)} queries × {js.num_pages} page(s), each every "
                      f"{js.interval_s() / 3600:.1f} h → ~{int(js.monthly_requests * 0.9)} of "
                      f"{js.monthly_requests} requests/month")
    else:
        console.print("Google Jobs (JSearch): off. Run `applypilot feeds set-key jsearch` to turn it on.")


@feeds_app.command("set-key")
def feeds_set_key(
    feed: str = typer.Argument(..., help="jsearch"),
    monthly_requests: int = typer.Option(None, help="Your plan's monthly quota (free 200, Pro 10000)"),
    provider: str = typer.Option(None, help="rapidapi (default) or openwebninja"),
) -> None:
    """Store an API key in ~/.applypilot/config.yaml (mode 600; typed hidden, never echoed)."""
    import os

    import yaml

    if feed != "jsearch":
        raise typer.BadParameter("only 'jsearch' takes a key")
    key = typer.prompt("JSearch API key", hide_input=True).strip()
    path = write_default_config()
    data = yaml.safe_load(path.read_text()) or {}
    js = data.setdefault("feeds", {}).setdefault("jsearch", {})
    js["api_key"] = key
    if monthly_requests:
        js["monthly_requests"] = monthly_requests
    if provider:
        js["provider"] = provider
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    os.chmod(path, 0o600)
    get_settings.cache_clear()
    cfg = get_settings().feeds.jsearch
    console.print(f"saved. {len(cfg.queries)} queries, each every {cfg.interval_s() / 3600:.1f} h "
                  f"({cfg.monthly_requests} requests/month). Test: `applypilot feeds test jsearch`")
    from applypilot import launchd

    if launchd.status() != "not installed":
        launchd.install()
        console.print("background agent restarted with the new key")


@feeds_app.command("test")
def feeds_test(feed: str = typer.Argument(..., help="jsearch"), query: str = "") -> None:
    """One JSearch request (costs 1 of the monthly quota): shows what it returns, stores nothing."""
    from applypilot.feeds.jsearch import JSearch

    s = get_settings()
    if feed != "jsearch" or not s.feeds.jsearch.api_key:
        raise typer.BadParameter("set a key first: applypilot feeds set-key jsearch")
    f = JSearch(query or s.feeds.jsearch.queries[0], s.feeds.jsearch)

    async def go():
        async with make_client(s) as client:
            return await f.fetch(client, None, None)

    r = asyncio.run(go())
    if r.status != 200:
        console.print(f"[red]HTTP {r.status}[/] {r.error or ''}")
        raise typer.Exit(1)
    t = Table("posted", "company", "title", "location", "best link")
    for lead in r.leads:
        best = resolver.best_url(lead.urls)
        t.add_row(notify.age(lead.posted_at).removeprefix("posted "), lead.company, lead.title, lead.location[:30],
                  best or f"[dim](aggregators only: {len(lead.urls)})[/]")
    console.print(t)


def _profile_email() -> str | None:
    import yaml

    path = home() / "profile.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    return (data or {}).get("email")


@gmail_app.command("login")
def gmail_login() -> None:
    """Sign in to Gmail in the browser (read-only); the token is saved to ~/.applypilot/gmail_token.json."""
    from applypilot import gmail

    try:
        profile = gmail.login()
    except gmail.GmailAuthError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    email = profile.get("emailAddress", "?")
    console.print(f"signed in as {email} (read-only). Check what arrived: `applypilot gmail status`")
    want = _profile_email()
    if want and email.lower() != want.lower():
        console.print(f"[yellow]note: profile.yaml uses {want}; alerts must go to the account signed in here[/]")
    from applypilot import launchd

    if launchd.status() != "not installed":
        launchd.install()
        console.print("background agent restarted: it now reads job-alert emails")


@gmail_app.command("logout")
def gmail_logout() -> None:
    """Revoke the Gmail sign-in and delete the saved token."""
    from applypilot import gmail

    console.print("signed out" if gmail.logout() else "not signed in")


@gmail_app.command("status")
def gmail_status(days: int = 14) -> None:
    """Account, sign-in age, and how many alert emails each site sent recently (reads headers only)."""
    from applypilot import gmail

    tok = gmail.token_info()
    if not tok:
        console.print("not signed in: run `applypilot gmail login`")
        raise typer.Exit(1)
    age_d = (time.time() - tok["obtained_at"]) / 86400
    console.print(f"account {tok.get('email')}, signed in {age_d:.1f} d ago. If the Google Cloud app is still in "
                  f"Testing, sign in again after {gmail.TESTING_TOKEN_DAYS} d.")

    async def go():
        async with make_client(get_settings()) as client:
            g = gmail.Gmail(client)
            rows = []
            for site, q in gmail.SITES.items():
                ids = await g.search(f"{q} newer_than:{days}d")
                newest = await g.headers(ids[0]) if ids else None
                rows.append((site, len(ids), newest))
            return rows

    try:
        rows = asyncio.run(go())
    except gmail.GmailAuthError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    t = Table("site", f"emails, last {days} d", "newest")
    for site, n, newest in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(newest["internal_date"])) if newest else ""
        t.add_row(site, f"{n}{'+' if n == 100 else ''}", when)
    console.print(t)


@gmail_app.command("test")
def gmail_test(days: int = 3) -> None:
    """Parse recent alert emails and show the job cards found (stores nothing, opens no links)."""
    import email
    from email import policy

    from applypilot import gmail
    from applypilot.feeds.email_alerts import PARSERS
    from applypilot.relevance import judge

    async def go():
        async with make_client(get_settings()) as client:
            g = gmail.Gmail(client)
            out = []
            for site, parse in PARSERS.items():
                for mid in await g.search(f"{gmail.SITES[site]} newer_than:{days}d"):
                    out += [(site, lead) for lead in parse(mid, email.message_from_bytes(
                        await g.raw(mid), policy=policy.default))]
            return out

    try:
        rows = asyncio.run(go())
    except gmail.GmailAuthError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    t = Table("site", "posted", "company", "title", "location", "relevant")
    for site, lead in rows:
        v = judge(Job(ATS.GREENHOUSE, "x", "1", lead.title, lead.company, lead.location, lead.country))
        t.add_row(site, notify.age(lead.posted_at).removeprefix("posted "), lead.company[:28], lead.title[:50],
                  lead.location[:24], "yes" if v.relevant else f"[dim]{v.reason}[/]")
    console.print(t)
    console.print(f"{len(rows)} cards from the last {days} d")


@gmail_app.command("samples")
def gmail_samples(per_site: int = 2, days: int = 30) -> None:
    """Save the newest alert emails per site as .eml files in ~/.applypilot/samples/ (parser fixtures)."""
    import os

    from applypilot import gmail

    out = home() / "samples"
    out.mkdir(mode=0o700, exist_ok=True)

    async def go():
        async with make_client(get_settings()) as client:
            g = gmail.Gmail(client)
            saved = []
            for site, q in gmail.SITES.items():
                for msg_id in await g.search(f"{q} newer_than:{days}d", max_results=per_site):
                    path = out / f"{site}-{msg_id}.eml"
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(await g.raw(msg_id))
                    saved.append(path.name)
            return saved

    try:
        saved = asyncio.run(go())
    except gmail.GmailAuthError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    console.print(f"saved {len(saved)} emails to {out}" + "".join(f"\n  {n}" for n in saved))


@app.command("resolve")
def resolve_cmd(
    url: str = typer.Argument("", help="A job link (ATS, career page, short link)"),
    company: str = typer.Option("", help="Company name; with --title, finds the job on its ATS board"),
    title: str = typer.Option("", help="Job title, e.g. from a LinkedIn alert"),
    location: str = "",
) -> None:
    """Find the employer-ATS job behind a link, or behind a company + title (e.g. a LinkedIn listing).
    Any new ATS board found is added to polling."""
    s = get_settings()
    conn = _conn()

    async def go():
        async with make_client(s) as client:
            if url:
                return await resolver.resolve(conn, client, url, company, "manual")
            return await resolver.resolve_by_name(
                conn, client, Poller(conn, s, client, push=False), company, title, location, "manual")

    if not url and not (company and title):
        raise typer.BadParameter("give a URL, or --company and --title")
    hit = asyncio.run(go())
    if hit is None:
        console.print("[yellow]not found[/]")
    elif isinstance(hit, resolver.Link):
        console.print(f"{hit.ats} · board {hit.slug or '?'} · job {hit.ext_id or '-'}"
                      f"{' · [green]new board added[/]' if hit.new_board else ''}\n  apply: {hit.apply_url}")
    else:
        console.print(f"{hit['company']}: {hit['title']} · {hit['location']} · {notify.age(hit['posted_at'])}"
                      f"\n  apply: {hit['apply_url'] or hit['url']}")


@app.command()
def jobs(hours: float = 24.0, fresh: bool = False, limit: int = 50) -> None:
    """Relevant jobs detected in the last N hours (--fresh: posted in the last N hours, incl. first scans)."""
    since = time.time() - hours * 3600
    col = "posted_at" if fresh else "detected_at"
    rows = _conn().execute(
        f"SELECT company, title, location, posted_at, detected_at, reason, apply_url, url, source FROM jobs"
        f" WHERE relevant=1 AND closed_at IS NULL AND {col} >= ? ORDER BY {col} DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    t = Table("posted", "company", "title", "location", "why", "via", "apply")
    for r in rows:
        t.add_row(notify.age(r["posted_at"]).removeprefix("posted "), r["company"], r["title"],
                  r["location"][:30], r["reason"].split(" · via ")[0], r["source"].removeprefix("github:"),
                  r["apply_url"] or r["url"])
    console.print(t)


@app.command()
def stats() -> None:
    """Boards per tier, job counts and detection latency (ATS publish -> our detection)."""
    conn = _conn()
    tiers = dict(conn.execute("SELECT tier, COUNT(*) FROM boards GROUP BY tier").fetchall())
    by_source = dict(conn.execute("SELECT source, COUNT(*) FROM boards GROUP BY source").fetchall())
    jobs_by_source = dict(conn.execute(
        "SELECT source, COUNT(*) FROM jobs WHERE closed_at IS NULL AND relevant=1 GROUP BY source").fetchall())
    by_ats = dict(conn.execute("SELECT ats, COUNT(*) FROM boards WHERE tier!='dead' GROUP BY ats").fetchall())
    open_jobs, relevant = conn.execute(
        "SELECT COUNT(*), SUM(relevant=1) FROM jobs WHERE closed_at IS NULL").fetchone()
    lat = [r[0] / 60 for r in conn.execute(
        "SELECT detected_at - posted_at FROM jobs WHERE is_baseline=0 AND relevant=1"
        " AND posted_at IS NOT NULL AND detected_at >= unixepoch() - 7*86400").fetchall()]
    console.print(f"boards by tier: {tiers}\nboards by ATS: {by_ats}\nboards by source: {by_source}\n"
                  f"open jobs: {open_jobs} (relevant {relevant})\nrelevant open jobs by first source: {jobs_by_source}")
    if lat:
        lat.sort()
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        console.print(f"detection latency, last 7 d (n={len(lat)}): p50 {statistics.median(lat):.1f} min, "
                      f"p95 {p95:.1f} min")
    else:
        console.print("detection latency: no new (post-baseline) relevant jobs yet")


@app.command()
def agent(action: str = typer.Argument(..., help="install | uninstall | status")) -> None:
    """Run the poller in the background via launchd (starts at login, restarts on crash)."""
    from applypilot import launchd

    if action == "install":
        console.print(f"installed {launchd.install()}\n  logs: {home() / 'run.log'}")
    elif action == "uninstall":
        launchd.uninstall()
        console.print("uninstalled")
    else:
        console.print(launchd.status())


@app.command("test-push")
def test_push() -> None:
    """Send a sample alert to check ntfy and macOS notifications."""
    s = get_settings()
    job = Job(ATS.GREENHOUSE, "example", "1", "Software Engineer, New Grad (test)", "ApplyPilot",
              "Pittsburgh, PA", url="https://ntfy.sh", posted_at=time.time() - 120)

    async def go():
        async with httpx.AsyncClient() as client:
            await notify.push(client, s, job, "new-grad tech role, US (test)")

    asyncio.run(go())
    console.print(f"sent to ntfy topic {s.ntfy_topic}")
