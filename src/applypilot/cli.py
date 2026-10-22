"""`applypilot` command line."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import shutil
import statistics
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

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
variants_app = typer.Typer(no_args_is_help=True, help="The 6 pre-built resume variants used for fit scoring.")
app.add_typer(variants_app, name="variants")
approvals_app = typer.Typer(no_args_is_help=True, help="One-tap approval from the phone (off by default).")
app.add_typer(approvals_app, name="approvals")
memory_app = typer.Typer(no_args_is_help=True, help="Answers you gave on forms, reused for the same question.")
app.add_typer(memory_app, name="memory")
gmail_app = typer.Typer(no_args_is_help=True, help="Read-only Gmail access for job-alert emails.")
app.add_typer(gmail_app, name="gmail")
console = Console(emoji=False)  # job ids like "ashby:snowflake:…" contain emoji codes
REPO = Path(__file__).resolve().parents[2]


def _job_ref(ref: str) -> str:
    """A job id, or a row number from the last `applypilot queue`."""
    if ref.isdigit():
        path = home() / "last_queue.json"
        ids = json.loads(path.read_text()) if path.exists() else []
        if not 1 <= int(ref) <= len(ids):
            raise typer.BadParameter(f"no row {ref} in the last queue (run `applypilot queue` first)")
        return ids[int(ref) - 1]
    return ref


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
            if s.prepare.enabled:
                from applypilot.apply import prepare

                loops.append(prepare.run_forever(conn, s, client))
            if s.approval.enabled and s.approval.topic and s.approval.secret:
                from applypilot.apply import approval

                async def apply_fn(job_id, _c=conn, _s=s, _cl=client):
                    return await approval.apply_job(_c, _s, _cl, job_id)

                loops.append(approval.listen(conn, s, client, apply_fn))
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


def _print_tailor(name: str, r) -> None:
    if r.error:
        console.print(f"[red]{name}: failed[/] {r.error}  ({r.seconds:.0f} s)")
        return
    cost = f", ${r.cost_usd:.2f}" if r.cost_usd is not None else ""
    console.print(f"{name}: score {r.score}  {r.pdf}  ({r.seconds / 60:.1f} min{cost})")
    for k in r.knockouts:
        console.print(f"   knockout: {k}")
    if r.unconfirmed:
        console.print(f"   unconfirmed tools (not added; confirm to use them): {', '.join(r.unconfirmed)}")


@variants_app.command("build")
def variants_build(only: Annotated[list[str] | None, typer.Option(help="build just these variants")] = None,
                   parallel: Annotated[int | None, typer.Option(help="concurrent claude sessions")] = None,
                   timeout: Annotated[int | None, typer.Option(help="seconds per variant (default tailor.timeout_s)")]
                   = None) -> None:
    """Tailor the master resume to each variant's archetype JD (Resume/variants/<name>/jd.txt) with
    `claude -p /resumetailor` in batch mode. Uses Claude usage: ~10-25 min per variant."""
    from applypilot import tailor

    s = get_settings()
    if timeout:
        s = s.model_copy(update={"tailor": s.tailor.model_copy(update={"timeout_s": timeout})})
    names = only or list(tailor.VARIANTS)
    missing = [n for n in names if not (tailor.variant_dir(s, n) / "jd.txt").exists()]
    if missing:
        raise typer.BadParameter(f"no jd.txt for: {', '.join(missing)}")
    sem = asyncio.Semaphore(parallel or s.tailor.parallel)

    async def one(n):
        async with sem:
            console.print(f"{n}: tailoring…")
            r = await tailor.run(s, tailor.variant_dir(s, n))
            _print_tailor(n, r)
            return r

    async def go():
        return await asyncio.gather(*(one(n) for n in names))

    results = asyncio.run(go())
    if any(r.error for r in results):
        raise typer.Exit(1)


@variants_app.command("list")
def variants_list() -> None:
    """Each variant's archetype JD, ATS score against it, and PDF."""
    from applypilot import tailor

    s = get_settings()
    t = Table("variant", "archetype JD", "score", "pdf")
    for n in tailor.VARIANTS:
        d = tailor.variant_dir(s, n)
        jd = (d / "jd.txt").read_text().splitlines()[:2] if (d / "jd.txt").exists() else []
        score = (d / "final_score.txt").read_text().strip() if (d / "final_score.txt").exists() else ""
        pdf = tailor.variant_pdf(s, n)
        t.add_row(n, " · ".join(reversed(jd)), score, str(pdf) if pdf.exists() else "[dim]not built[/]")
    console.print(t)


@app.command("tailor")
def tailor_cmd(job_id: str) -> None:
    """Tailor the resume to one stored job (claude -p /resumetailor, batch mode; ~10-25 min of Claude usage)."""
    from applypilot import tailor

    s = get_settings()
    conn = _conn()
    row = db.get_job(conn, job_id := _job_ref(job_id))
    if row is None:
        raise typer.BadParameter(f"no job {job_id}")
    if not row["description"]:
        raise typer.BadParameter("this job has no description to tailor to (run `screen --fetch-days`)")
    out = tailor.resume_dir(s) / "tailored" / tailor.slug(row["company"], row["title"])
    tailor.write_jd(out, row["title"], row["company"], row["url"], row["description"])
    r = asyncio.run(tailor.run(s, out))
    _print_tailor(row["company"], r)
    if r.error:
        raise typer.Exit(1)
    db.set_tailored(conn, job_id, str(r.pdf), r.score)


@app.command("plan")
def plan_cmd(job_id: str) -> None:
    """Show how ApplyPilot would fill this job's application: every field, its answer and source, the resume
    to attach, and what needs you. Reads the form only; nothing is filled or sent."""
    from applypilot.apply import answers, forms

    s = get_settings()
    row = db.get_job(_conn(), job_id := _job_ref(job_id))
    if row is None:
        raise typer.BadParameter(f"no job {job_id}")

    async def go():
        async with make_client(s) as client:
            return await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])

    try:
        form = asyncio.run(go())
    except (httpx.HTTPError, ValueError) as e:
        console.print(f"[red]can't read the form:[/] {e}")
        raise typer.Exit(1) from None
    resume = answers.resume_for(s, row)
    plan = answers.plan(form, resume, row["company"])
    t = Table("", "question", "answer", "from")
    for a in plan:
        if a.source == "skip":
            continue
        mark = "[yellow]?[/]" if a.source == "needs_you" else ("*" if a.field.required else "")
        value = a.note if a.source == "needs_you" else (", ".join(a.value) if isinstance(a.value, list) else a.value)
        t.add_row(mark, a.field.label[:70], str(value or "")[:60], a.source)
    console.print(f"[bold]{row['company']}: {row['title']}[/]  {form.url}")
    console.print(t)
    needs = [a for a in plan if a.source == "needs_you"]
    req = [a for a in plan if a.field.required]
    console.print(f"required fields answered: {sum(a.source not in ('needs_you', 'skip') for a in req)}/{len(req)}"
                  f" · needs you: {len(needs)} · resume: {resume or 'none found'}"
                  + (f" · [yellow]{form.captcha} on the page: you solve it[/]" if form.captcha else ""))


@app.command("fill")
def fill_cmd(job_id: str,
             with_resume: Annotated[bool, typer.Option(help="attach the resume (Lever/Ashby upload it at once)")]
             = False,
             headless: bool = False, keep_open: bool = False) -> None:
    """Dry run: open the job's application in Chrome, fill every answer from the plan, read each field back and
    screenshot it. Never submits."""
    from applypilot.apply import answers, filler, forms

    s = get_settings()
    row = db.get_job(_conn(), job_id := _job_ref(job_id))
    if row is None:
        raise typer.BadParameter(f"no job {job_id}")

    async def go():
        async with make_client(s) as client:
            form = await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])
        plan = answers.plan(form, answers.resume_for(s, row), row["company"])
        return await filler.fill(row["ats"], form.url, plan, with_resume=with_resume, headless=headless,
                                 keep_open=keep_open)

    results, shot = asyncio.run(go())
    t = Table("status", "question", "wanted", "read back / note")
    colors = {"filled": "green", "mismatch": "red", "failed": "red", "needs_you": "yellow", "skipped": "dim"}
    for r in results:
        if r.status == "skipped" and not r.wanted:
            continue
        t.add_row(f"[{colors[r.status]}]{r.status}[/]", r.label[:60], r.wanted[:40], (r.got or r.error)[:50])
    console.print(t)
    n = collections.Counter(r.status for r in results)
    console.print(f"filled {n['filled']} · mismatch {n['mismatch']} · failed {n['failed']} · needs you "
                  f"{n['needs_you']} · screenshot {shot}  (nothing was submitted)")


@app.command("apply")
def apply_cmd(job_id: str, again: bool = False) -> None:
    """Apply to one job with you at the Mac: Chrome fills the form and attaches the resume, outlines what needs
    you, and submits only when you type 'submit'. Logged in `applications`; per-company cap enforced."""
    from applypilot.apply import answers, drafts, forms, prepare, submit

    s = get_settings()
    conn = _conn()
    row = db.get_job(conn, job_id := _job_ref(job_id))
    if row is None:
        raise typer.BadParameter(f"no job {job_id}")
    if (prev := db.applied_to(conn, job_id)) and not again:
        when = time.strftime("%Y-%m-%d", time.localtime(prev["started_at"]))
        console.print(f"already applied ({prev['status']}, {when}). Use --again to apply anyway.")
        raise typer.Exit(1)
    if reason := submit.cap_reason(conn, s, row["company"]):
        console.print(f"[yellow]not applying: {reason}[/] (apply.max_per_company_30d in config.yaml)")
        raise typer.Exit(1)

    async def go():
        async with make_client(s) as client:
            form = await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])
        plan = answers.plan(form, answers.resume_for(s, row), row["company"])
        found = prepare.stored_drafts(conn, row["id"], plan)  # drafted in the background, wording unchanged
        have = {d.key for d in found}
        if s.apply.drafts and any(drafts.draftable(a) and a.field.key not in have for a in plan):
            with console.status("drafting written answers from your verified facts…"):
                rest = [a for a in plan if a.field.key not in have]
                found += await drafts.draft(s, rest, row["company"], row["title"], row["description"] or "")
        plan = drafts.apply_drafts(plan, found)
        if found:
            for d in found:
                console.print(f"[{'green' if d.ok else 'yellow'}]draft[/] {d.label[:70]}"
                              + ("" if d.ok else f"  [yellow]CHECK: {'; '.join(d.issues)}[/]"))
        res = await submit.apply(conn, s, row, form, plan, say=console.print)
        if res["status"] == "submitted":
            async with httpx.AsyncClient() as client:
                await notify.message(client, s, f"Applied: {row['company']}", f"{row['title']} · {res['confirmation']}")
        return res

    res = asyncio.run(go())
    color = {"submitted": "green", "unconfirmed": "yellow"}.get(res["status"], "red")
    console.print(f"[{color}]{res['status']}[/] {res.get('confirmation') or res.get('error') or ''}")
    if res.get("screenshot_after"):
        console.print(f"screenshots: {res['screenshot_filled']}  {res['screenshot_after']}")


@app.command("queue")
def queue_cmd(limit: int = 20, hours: float = 24 * 14) -> None:
    """Best jobs to apply to next: open, relevant, on Greenhouse/Lever/Ashby, not applied to, under the company cap."""
    from applypilot.apply import submit

    s = get_settings()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE relevant=1 AND closed_at IS NULL AND ats IN ('greenhouse','lever','ashby')"
        " AND score IS NOT NULL AND posted_at >= ? AND id NOT IN (SELECT job_id FROM applications WHERE status IN"
        " ('submitted','unconfirmed')) ORDER BY score DESC LIMIT ?", (time.time() - hours * 3600, limit * 3)).fetchall()
    t = Table("#", "score", "posted", "company", "title", "resume", "ready")
    t.add_column("job id", overflow="fold")  # never cut: it's what you copy
    picked = []
    for r in rows:
        if submit.cap_reason(conn, s, r["company"]):
            continue
        picked.append(r["id"])
        prep = db.get_prepared(conn, r["id"])
        if prep is None:
            ready = "[dim]–[/]"
        elif prep["error"]:
            ready = "[red]form unreadable[/]"
        else:
            n, k = prep["needs_count"] or 0, len(json.loads(prep["drafts"] or "[]"))
            ready = ("[green]ready[/]" if n == 0 else f"{n} for you") + (f" · {k} drafted" if k else "")
        t.add_row(str(len(picked)), f"{r['score']:.0f}", notify.age(r["posted_at"]).removeprefix("posted "),
                  r["company"][:20], r["title"][:45], f"{r['variant'] or 'master'} {r['fit'] or ''}", ready, r["id"])
        if len(picked) >= limit:
            break
    (home() / "last_queue.json").write_text(json.dumps(picked))
    console.print(t)
    console.print("apply with: applypilot apply <#>  (the number in the first column, or the full job id)"
                  "   preview: applypilot plan <#>")


@app.command("applications")
def applications_cmd(limit: int = 30) -> None:
    """Applications so far, newest first."""
    rows = _conn().execute("SELECT * FROM applications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    t = Table("when", "status", "company", "title", "you completed", "confirmation / error")
    for r in rows:
        done = len(json.loads(r["completed_by_user"] or "[]"))
        t.add_row(time.strftime("%m-%d %H:%M", time.localtime(r["started_at"])), r["status"], r["company"][:20],
                  r["title"][:40], str(done), (r["confirmation"] or r["error"] or "")[:50])
    console.print(t)


@memory_app.command("list")
def memory_list() -> None:
    """Every remembered answer (stored in ~/.applypilot/answer_memory.yaml)."""
    from applypilot.apply import memory

    t = Table("#", "question", "answer", "only for", "learned")
    for i, m in enumerate(memory.load(), 1):
        t.add_row(str(i), m.example[:70], m.answer[:40], m.company or "any company", m.learned)
    console.print(t)
    console.print(f"edit or delete entries in {memory.path()}, or: applypilot memory forget <#>")


@memory_app.command("forget")
def memory_forget(number: int) -> None:
    """Delete one remembered answer by its number in `memory list`."""
    from applypilot.apply import memory

    items = memory.load()
    if not 1 <= number <= len(items):
        raise typer.BadParameter(f"no entry {number}")
    gone = items.pop(number - 1)
    memory.save(items)
    console.print(f"forgot: {gone.example[:80]} -> {gone.answer}")


@app.command("draft")
def draft_cmd(job_id: str) -> None:
    """Preview drafted written answers for a job (from your verified facts) and their grounding checks.
    Opens no browser and sends nothing to the employer."""
    from applypilot.apply import answers, drafts, forms

    s = get_settings()
    row = db.get_job(_conn(), job_id := _job_ref(job_id))
    if row is None:
        raise typer.BadParameter(f"no job {job_id}")

    async def go():
        async with make_client(s) as client:
            form = await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])
        plan = answers.plan(form, None, row["company"])
        todo = [a for a in plan if drafts.draftable(a)]
        if not todo:
            return [], []
        with console.status(f"drafting {len(todo)} answer(s) from your verified facts…"):
            return todo, await drafts.draft(s, plan, row["company"], row["title"], row["description"] or "")

    todo, found = asyncio.run(go())
    if not todo:
        console.print("no written questions to draft on this form")
        return
    if not found:
        console.print("[red]no drafts came back[/] (is the resume fact base at tailor.resume_dir/CLAUDE.md?)")
        raise typer.Exit(1)
    for d in found:
        console.rule(d.label[:100])
        console.print(d.text)
        console.print("[green]grounding checks passed[/]" if d.ok else "[yellow]CHECK:[/] " + "; ".join(d.issues))
        if d.facts_used:
            console.print("[dim]facts used: " + " | ".join(f[:60] for f in d.facts_used) + "[/]")


@app.command("prepare")
def prepare_cmd(limit: int = 3) -> None:
    """Prepare the next high-scoring jobs now (read form, plan, draft essays). Sends nothing. The background
    agent does this every few minutes on its own."""
    from applypilot.apply import prepare

    _setup_logging()
    s = get_settings()
    conn = _conn()

    async def go():
        async with make_client(s) as client:
            out = []
            for r in prepare.candidates(conn, s, limit):
                with console.status(f"preparing {r['company']}: {r['title'][:50]}…"):
                    out.append((r, await prepare.prepare(conn, s, client, r)))
            return out

    done = asyncio.run(go())
    if not done:
        console.print(f"nothing to prepare (score ≥ {s.prepare.min_score}, posted ≤ {s.prepare.max_age_days} d,"
                      " not applied, not prepared)")
    for r, p in done:
        if p["error"]:
            console.print(f"[red]{r['company']}[/]: {p['error']}")
            continue
        k = len(json.loads(p["drafts"] or "[]"))
        console.print(f"{r['company']}: {r['title'][:60]} · {p['needs_count']} for you · {k} drafted")


def _save_config(update) -> None:
    import os

    import yaml

    path = write_default_config()
    data = yaml.safe_load(path.read_text()) or {}
    update(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    os.chmod(path, 0o600)
    get_settings.cache_clear()


@approvals_app.command("enable")
def approvals_enable(min_supervised: Annotated[int | None, typer.Option(
        help="confirmed applications through `apply` at the Mac before phone approvals switch on")] = None) -> None:
    """Turn on one-tap approval: creates a private approval topic and a signing key (config.yaml, mode 600)."""
    import secrets

    from applypilot import launchd

    def upd(d):
        a = d.setdefault("approval", {})
        a["enabled"] = True
        a.setdefault("topic", f"applypilot-ok-{secrets.token_urlsafe(12)}")
        a.setdefault("secret", secrets.token_urlsafe(32))
        if min_supervised is not None:
            a["min_supervised"] = min_supervised

    _save_config(upd)
    s = get_settings()
    if launchd.status() != "not installed":
        launchd.install()
    console.print(f"phone approval enabled. Apply buttons start after {s.approval.min_supervised} confirmed "
                  "applications through `applypilot apply` at the Mac, and only for forms that need nothing from you.\n"
                  "Nothing to install on the phone: the button posts to a private topic the Mac listens to.")


@approvals_app.command("disable")
def approvals_disable() -> None:
    """Turn off one-tap approval (keeps the topic and key, so re-enabling doesn't change them)."""
    from applypilot import launchd

    _save_config(lambda d: d.setdefault("approval", {}).update(enabled=False))
    if launchd.status() != "not installed":
        launchd.install()
    console.print("phone approval disabled")


@approvals_app.command("status")
def approvals_status() -> None:
    """Whether phone approval is active, why not, and recent approvals."""
    from applypilot.apply import approval

    s = get_settings()
    conn = _conn()
    on, why = approval.active(conn, s)
    console.print(f"phone approval: {'[green]active[/]' if on else '[yellow]inactive[/]'} ({why}); "
                  f"supervised applications: {approval.supervised(conn)}/{s.approval.min_supervised}; "
                  f"offers today: {approval.offers_today(conn)}/{s.approval.max_offers_per_day}")
    t = Table("received", "job", "result")
    for r in conn.execute("SELECT * FROM approvals ORDER BY received_at DESC LIMIT 15"):
        t.add_row(time.strftime("%m-%d %H:%M", time.localtime(r["received_at"])), r["job_id"], r["result"] or "…")
    console.print(t)


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


_VISA = {"offers": "sponsors", "no": "OPT path", "unknown": ""}


@app.command()
def jobs(hours: float = 24.0, fresh: bool = False, limit: int = 50, by_score: bool = False) -> None:
    """Relevant jobs detected in the last N hours (--fresh: posted in the last N hours, incl. first scans;
    --by-score: highest priority first)."""
    since = time.time() - hours * 3600
    col = "posted_at" if fresh else "detected_at"
    order = "score DESC NULLS LAST, posted_at DESC" if by_score else f"{col} DESC"
    rows = _conn().execute(
        f"SELECT company, title, location, posted_at, detected_at, reason, apply_url, url, source, score,"
        f" sponsorship, everify FROM jobs WHERE relevant=1 AND closed_at IS NULL AND {col} >= ? ORDER BY {order}"
        f" LIMIT ?",
        (since, limit),
    ).fetchall()
    t = Table("score", "posted", "company", "title", "location", "visa", "via", "apply")
    for r in rows:
        visa = " · ".join(x for x in (_VISA.get(r["sponsorship"] or "unknown", ""), "E-Verify" if r["everify"] else "")
                          if x)
        t.add_row("" if r["score"] is None else f"{r['score']:.0f}", notify.age(r["posted_at"]).removeprefix("posted "),
                  r["company"], r["title"], r["location"][:30], visa, r["source"].removeprefix("github:"),
                  r["apply_url"] or r["url"])
    console.print(t)


@app.command("screen")
def screen_cmd(all_jobs: bool = typer.Option(False, "--all", help="re-screen jobs screened before"),
               fetch_days: float = typer.Option(0, help="first fetch missing ATS descriptions for open relevant "
                                                        "jobs posted in the last N days")) -> None:
    """Run the description filters over stored relevant jobs: hard drops, visa signals, priority score."""
    import collections

    from applypilot import rank
    from applypilot.sources import SOURCES

    _setup_logging()
    conn = _conn()
    if fetch_days:
        todo = [db.row_job(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE relevant=1 AND closed_at IS NULL AND description='' AND posted_at >= ?",
            (time.time() - fetch_days * 86400,)).fetchall() if r["ats"] in SOURCES]

        async def go():
            sem = asyncio.Semaphore(6)
            async with make_client(get_settings()) as client:
                async def one(j):
                    async with sem:
                        try:
                            await SOURCES[j.ats].details(client, j)
                            db.set_description(conn, j.id, j.description)
                        except (httpx.HTTPError, ValueError):
                            pass
                await asyncio.gather(*(one(j) for j in todo))

        with console.status(f"fetching {len(todo)} descriptions"):
            asyncio.run(go())
    from applypilot.relevance import judge

    # titles judged under older rules (e.g. before the clearance-title drop) are re-checked first
    retitled = 0
    for r in conn.execute("SELECT * FROM jobs WHERE relevant=1 AND closed_at IS NULL").fetchall():
        v = judge(db.row_job(r))
        if not v.relevant:
            db.set_relevance(conn, r["id"], False, v.reason)
            retitled += 1
    if retitled:
        console.print(f"{retitled} jobs no longer pass the title/location gate")
    where = "" if all_jobs else " AND screened_at IS NULL"
    rows = conn.execute(f"SELECT * FROM jobs WHERE relevant=1 AND closed_at IS NULL AND description != ''{where}"
                        ).fetchall()
    drops: collections.Counter = collections.Counter()
    now = time.time()
    for r in rows:
        keep, _ = rank.screen(conn, db.row_job(r), now, get_settings())
        if not keep:
            drops[db.get_job(conn, r["id"])["reason"].removeprefix("hard drop: ")] += 1
    console.print(f"screened {len(rows)} jobs: kept {len(rows) - sum(drops.values())}, hard drops "
                  f"{sum(drops.values())}")
    for reason, n in drops.most_common():
        console.print(f"  {n:5}  {reason}")
    left = conn.execute("SELECT COUNT(*) FROM jobs WHERE relevant=1 AND closed_at IS NULL AND description=''"
                        ).fetchone()[0]
    console.print(f"{left} relevant open jobs have no description yet (use --fetch-days for ATS ones)")


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
    # board polls and feeds measure different things (our polling vs. how late a list or alert adds a job);
    # a job first published > 48 h before we saw it was re-listed or added late, not posted just now
    lags = conn.execute(
        "SELECT source = 'ats', (detected_at - posted_at) / 60 FROM jobs WHERE is_baseline=0 AND relevant=1"
        " AND posted_at IS NOT NULL AND detected_at >= unixepoch() - 7*86400").fetchall()
    late = sum(1 for _, m in lags if m > 48 * 60)
    lat = sorted(m for ats, m in lags if ats and m <= 48 * 60)
    feed_lat = sorted(m for ats, m in lags if not ats and m <= 48 * 60)
    console.print(f"boards by tier: {tiers}\nboards by ATS: {by_ats}\nboards by source: {by_source}\n"
                  f"open jobs: {open_jobs} (relevant {relevant})\nrelevant open jobs by first source: {jobs_by_source}")
    dropped = conn.execute("SELECT COUNT(*) FROM jobs WHERE reason LIKE 'hard drop:%' AND closed_at IS NULL"
                           ).fetchone()[0]
    screened = conn.execute("SELECT COUNT(*) FROM jobs WHERE relevant=1 AND closed_at IS NULL AND screened_at"
                            " IS NOT NULL").fetchone()[0]
    console.print(f"description screening: {screened} relevant jobs screened, {dropped} open jobs hard-dropped")
    for name, xs in (("board polls", lat), ("feeds", feed_lat)):
        if xs:
            p95 = xs[min(len(xs) - 1, int(0.95 * len(xs)))]
            console.print(f"detection latency via {name}, last 7 d (n={len(xs)}): p50 "
                          f"{statistics.median(xs):.1f} min, p95 {p95:.1f} min")
        else:
            console.print(f"detection latency via {name}: no new (post-baseline) relevant jobs yet")
    if late:
        console.print(f"  (not counted: {late} jobs first published > 48 h before we saw them: re-listed or "
                      f"added late)")


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
