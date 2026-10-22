"""One-tap approval from the phone.

Offer: when background preparation finds a job whose form needs nothing from the user (no legal
notice, no draft to review, no CAPTCHA, every field answered), and phone approval is on, a push
offers an **Apply** button. The button is an ntfy `http` action: the phone POSTs `"<job id> <token>"`
to a second private topic. The token is an HMAC of the job id with an expiry, so knowing the topic
alone can't approve anything.

Listen: the Mac agent streams that topic (`/json`, catching up on messages sent while it was asleep;
ntfy keeps them about 12 h), checks each token once, and runs the unattended apply.

Gate: phone approval switches on only after `approval.min_supervised` applications were confirmed
through `apply` at the Mac, so the filling has been watched on real forms first.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

from applypilot import db
from applypilot.config import Settings, home

log = logging.getLogger("applypilot.approval")


def sign(secret: str, job_id: str, now: float, ttl_h: int) -> str:
    exp = int(now + ttl_h * 3600)
    mac = hmac.new(secret.encode(), f"{job_id}|{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{exp}.{mac}"


def verify(secret: str, job_id: str, token: str, now: float) -> bool:
    try:
        exp, mac = token.split(".", 1)
        expired = int(exp) < now
    except ValueError:
        return False
    good = hmac.new(secret.encode(), f"{job_id}|{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return not expired and hmac.compare_digest(mac, good)


def supervised(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM applications WHERE status='submitted' AND approved_via='mac'"
                        ).fetchone()[0]


def offers_today(conn) -> int:
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return conn.execute("SELECT COUNT(*) FROM prepared WHERE offered_at >= ?", (start,)).fetchone()[0]


def active(conn, settings: Settings) -> tuple[bool, str]:
    a = settings.approval
    if not (a.enabled and a.topic and a.secret):
        return False, "phone approval is off (applypilot approvals enable)"
    if (n := supervised(conn)) < a.min_supervised:
        return False, f"{n} of {a.min_supervised} supervised applications so far"
    return True, "on"


def eligible(prep) -> tuple[bool, str]:
    """A prepared job can be approved from the phone only when its form needs nothing from the user."""
    if prep is None or prep["error"]:
        return False, "not prepared"
    if prep["captcha"]:
        return False, f"{prep['captcha']} on the page"
    if json.loads(prep["drafts"] or "[]"):
        return False, "has drafted answers to review"
    needs = [n for n in json.loads(prep["needs"] or "[]") if n.get("required")]
    if needs:
        return False, f"{len(needs)} question(s) need you"
    return True, "ready"


async def offer(conn, settings: Settings, client: httpx.AsyncClient, row, prep) -> bool:
    """Send the one-tap push for a ready job (once per job, capped per day)."""
    on, _ = active(conn, settings)
    ok, _ = eligible(prep)
    if not (on and ok) or prep["offered_at"] or offers_today(conn) >= settings.approval.max_offers_per_day:
        return False
    token = sign(settings.approval.secret, row["id"], time.time(), settings.approval.token_ttl_h)
    server = settings.ntfy_server.rstrip("/")
    payload = {
        "topic": settings.ntfy_topic,
        "title": f"Ready to apply: {row['company']}"[:120],
        "message": f"{row['title']}\nscore {row['score']:.0f} · resume {row['variant'] or 'master'}"
                   " · nothing needs you.\nTap Apply to submit from the Mac (it re-checks the form first).",
        "priority": 4,
        "tags": ["rocket"],
        "actions": [
            {"action": "http", "label": "Apply", "url": f"{server}/{settings.approval.topic}", "method": "POST",
             "body": f"{row['id']} {token}", "clear": True},
            {"action": "view", "label": "Posting", "url": row["url"] or row["apply_url"]},
        ],
    }
    try:
        r = await client.post(server, content=json.dumps(payload).encode())
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("offer push failed for %s: %r", row["id"], e)
        return False
    conn.execute("UPDATE prepared SET offered_at=? WHERE job_id=?", (time.time(), row["id"]))
    return True


def _cursor_path():
    return home() / "approval_cursor.json"


def parse(line: str) -> tuple[str, str, str] | None:
    """(message id, job id, token) from one ntfy JSON stream line, or None."""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if ev.get("event") != "message":
        return None
    parts = (ev.get("message") or "").split()
    return (ev["id"], parts[0], parts[1]) if len(parts) == 2 else None


async def handle(conn, settings: Settings, client: httpx.AsyncClient, msg_id: str, job_id: str, token: str,
                 apply_fn) -> str:
    """Check one approval message and apply. Returns the stored result."""
    if conn.execute("SELECT 1 FROM approvals WHERE message_id=?", (msg_id,)).fetchone():
        return "duplicate"
    conn.execute("INSERT INTO approvals (message_id, job_id, received_at) VALUES (?,?,?)", (msg_id, job_id,
                                                                                            time.time()))
    if not verify(settings.approval.secret, job_id, token, time.time()):
        result = "refused: bad or expired token"
    elif not active(conn, settings)[0]:
        result = f"refused: {active(conn, settings)[1]}"
    else:
        result = await apply_fn(job_id)
    conn.execute("UPDATE approvals SET result=? WHERE message_id=?", (result, msg_id))
    log.info("approval %s for %s -> %s", msg_id, job_id, result)
    return result


async def listen(conn, settings: Settings, client: httpx.AsyncClient, apply_fn) -> None:
    """Stream the approval topic forever; one application at a time."""
    url = f"{settings.ntfy_server.rstrip('/')}/{settings.approval.topic}/json"
    lock = asyncio.Lock()
    while True:
        since = json.loads(_cursor_path().read_text()).get("since", "12h") if _cursor_path().exists() else "12h"
        try:
            async with client.stream("GET", url, params={"since": since}, timeout=None) as r:
                async for line in r.aiter_lines():
                    got = parse(line)
                    if got is None:
                        continue
                    async with lock:
                        await handle(conn, settings, client, *got, apply_fn)
                    _cursor_path().write_text(json.dumps({"since": got[0]}))
        except (httpx.HTTPError, OSError) as e:
            log.warning("approval stream dropped: %r; reconnecting", e)
        await asyncio.sleep(10)


async def apply_job(conn, settings: Settings, client: httpx.AsyncClient, job_id: str) -> str:
    """Unattended apply for an approved job: re-check everything, then fill and submit only if all verify."""
    from applypilot import notify
    from applypilot.apply import answers, forms, submit

    row = db.get_job(conn, job_id)
    if row is None:
        return "refused: unknown job"
    if db.applied_to(conn, job_id):
        return "refused: already applied"
    if reason := submit.cap_reason(conn, settings, row["company"]):
        return f"refused: {reason}"
    try:
        form = await forms.fetch(client, row["ats"], row["slug"], row["ext_id"], row["apply_url"])
    except (httpx.HTTPError, ValueError) as e:
        return f"failed: form unreadable ({type(e).__name__})"
    plan = answers.plan(form, answers.resume_for(settings, row), row["company"])
    needs = [a for a in plan if a.source == "needs_you" and a.field.required]
    if form.captcha or needs:
        what = form.captcha or f"{len(needs)} question(s) now need you"
        await notify.message(client, settings, f"Needs you at the Mac: {row['company']}",
                             f"{row['title']}: the form changed ({what}). Run: applypilot apply {job_id}")
        return f"refused: {what}"
    res = await submit.apply_unattended(conn, settings, row, form, plan)
    title = {"submitted": "Applied", "unconfirmed": "Check: no confirmation"}.get(res["status"], "Not applied")
    detail = res.get("confirmation") or res.get("error") or ""
    await notify.message(client, settings, f"{title}: {row['company']}", f"{row['title']} · {detail}"[:300],
                         priority="high" if res["status"] != "submitted" else "default")
    return res["status"] if res["status"] in ("submitted", "unconfirmed") else f"failed: {detail[:120]}"
