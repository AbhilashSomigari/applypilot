"""Background preparation: do the slow parts of an application before the user asks for it.

For new high-scoring jobs (score ≥ `prepare.min_score`, posted in the last `prepare.max_age_days`, on
Greenhouse / Lever / Ashby, not applied to), the agent reads the form, plans the answers and drafts the
written answers. It stores the drafts and a summary (what will need the user) in `prepared`.

Nothing is filled in a browser and nothing is sent. A filled page can't be kept between runs, and
Lever/Ashby upload the resume as soon as it's attached. `apply` re-reads the form (one request),
re-plans (instant, so profile and memory changes apply) and reuses the stored drafts for questions
whose wording is unchanged. Drafting uses Claude usage, so it's capped per day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict

import httpx

from applypilot import db
from applypilot.apply import answers, drafts, forms
from applypilot.apply.drafts import Draft
from applypilot.config import Settings

log = logging.getLogger("applypilot.prepare")


def form_hash(form: forms.Form) -> str:
    return hashlib.sha1(json.dumps([(f.key, f.label) for f in form.fields]).encode()).hexdigest()[:16]


def candidates(conn, settings: Settings, limit: int) -> list:
    p = settings.prepare
    return conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN prepared p ON p.job_id = j.id"
        " WHERE j.relevant=1 AND j.closed_at IS NULL AND j.ats IN ('greenhouse','lever','ashby') AND j.score >= ?"
        " AND j.posted_at >= ? AND j.description != ''"
        " AND j.id NOT IN (SELECT job_id FROM applications WHERE status IN ('submitted','unconfirmed'))"
        " AND (p.job_id IS NULL OR (p.error IS NOT NULL AND p.prepared_at < ?))"
        " ORDER BY j.score DESC LIMIT ?",
        (p.min_score, time.time() - p.max_age_days * 86400, time.time() - 86400, limit)).fetchall()


def drafts_today(conn) -> int:
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return conn.execute("SELECT COUNT(*) FROM prepared WHERE drafted=1 AND prepared_at >= ?", (start,)).fetchone()[0]


async def prepare(conn, settings: Settings, client: httpx.AsyncClient, row) -> dict:
    """Prepare one job; returns the stored row as a dict."""
    now = time.time()
    try:
        form = await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])
    except (httpx.HTTPError, ValueError) as e:
        db.save_prepared(conn, row["id"], now, error=f"{type(e).__name__}: {str(e)[:200]}")
        return dict(db.get_prepared(conn, row["id"]))
    plan = answers.plan(form, answers.resume_for(settings, row), row["company"])
    found: list[Draft] = []
    drafted = 0
    want = [a for a in plan if drafts.draftable(a)]
    if settings.apply.drafts and want and drafts_today(conn) < settings.prepare.max_drafted_per_day:
        found = await drafts.draft(settings, plan, row["company"], row["title"], row["description"] or "")
        drafted = 1
    plan = drafts.apply_drafts(plan, found)
    needs = [{"label": a.field.label, "note": a.note, "required": a.field.required}
             for a in plan if a.source in ("needs_you", "draft") and (a.field.required or a.source == "draft")]
    db.save_prepared(conn, row["id"], now, form_hash=form_hash(form), needs=json.dumps(needs),
                     needs_count=sum(1 for n in needs if not n["note"].startswith("draft")),
                     drafts=json.dumps([asdict(d) for d in found]), drafted=drafted,
                     captcha=form.captcha)
    return dict(db.get_prepared(conn, row["id"]))


def stored_drafts(conn, job_id: str, plan: list) -> list[Draft]:
    """Drafts prepared earlier whose question wording still matches the live form."""
    row = db.get_prepared(conn, job_id)
    if row is None or not row["drafts"]:
        return []
    labels = {a.field.key: a.field.label for a in plan}
    return [Draft(**d) for d in json.loads(row["drafts"]) if labels.get(d["key"]) == d["label"]]


async def run_once(conn, settings: Settings, client: httpx.AsyncClient) -> int:
    rows = candidates(conn, settings, settings.prepare.per_cycle)
    from applypilot.apply import approval

    for r in rows:
        res = await prepare(conn, settings, client, r)
        if await approval.offer(conn, settings, client, r, db.get_prepared(conn, r["id"])):
            log.info("offered one-tap apply for %s (%s)", r["id"], r["company"])
        log.info("prepared %s (%s): needs %s, drafts %s%s", r["id"], r["company"], res.get("needs_count"),
                 len(json.loads(res.get("drafts") or "[]")), f", error {res['error']}" if res.get("error") else "")
    return len(rows)


async def run_forever(conn, settings: Settings, client: httpx.AsyncClient) -> None:
    import asyncio

    while True:
        try:
            await run_once(conn, settings, client)
        except Exception as e:  # preparing is best-effort; it must never stop polling
            log.warning("prepare cycle failed: %r", e)
        await asyncio.sleep(settings.prepare.interval_s)
