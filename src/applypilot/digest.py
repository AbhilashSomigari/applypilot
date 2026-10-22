"""The daily digest, plus response tracking from Gmail.

Responses: for each application submitted in the last `digest.response_window_days`, Gmail (read-only)
is searched for later mail that names the company, excluding job-alert senders. Each message is
classified once by its subject and snippet (rejection > assessment > interview > received), and the
application's `outcome` moves forward (received → assessment → interview; rejected is final).

Digest, once a day at `digest.hour`:
- applied in the last day, and unconfirmed or failed ones to check;
- responses since the last digest;
- prepared jobs ready for the user;
- what the pipeline found.
A short version is pushed; the full one is saved to `~/.applypilot/digests/YYYY-MM-DD.md`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from applypilot import db, gmail, notify
from applypilot.config import Settings, home

log = logging.getLogger("applypilot.digest")

KINDS = (  # first match wins: a rejection often mentions "interview", an invite can say "thank you for applying"
    ("rejected", re.compile(r"unfortunately|not (to )?(be )?(move|moving) forward|decided to (move forward|proceed"
                            r"|pursue) (with )?other|no longer (under consideration|considering)"
                            r"|position has been filled|will not be (moving|proceeding)|regret to inform|not selected"
                            r"|other candidates", re.I)),
    ("assessment", re.compile(r"assessment|coding (challenge|exercise|test)|hackerrank|codesignal|take[- ]home"
                              r"|online test|codility|karat", re.I)),
    ("interview", re.compile(r"interview|schedule (a|your|some) (call|chat|time)|next steps|your availability"
                             r"|speak with you|phone screen|recruiter (call|screen)|calendly|meet with", re.I)),
    ("received", re.compile(r"thank(s| you) for (applying|your application|your interest)|application (was |has been )?"
                            r"(received|submitted)|we.ve received your application|received your application", re.I)),
)
RANK = {"received": 1, "assessment": 2, "interview": 3, "rejected": 4}
_ALERT_SENDERS = ("-from:(indeed.com OR linkedin.com OR glassdoor.com OR ziprecruiter.com OR dice.com"
                  " OR joinhandshake.com)")


def classify(subject: str, snippet: str) -> str:
    text = f"{subject}\n{snippet}"
    return next((kind for kind, rx in KINDS if rx.search(text)), "other")


def _company_query(company: str) -> str:
    name = re.sub(r",? (inc|llc|corp|co|ltd)\.?$", "", company, flags=re.I).strip()
    return f'"{name}"'


async def scan_responses(conn, settings: Settings, client: httpx.AsyncClient, now: float | None = None) -> list[dict]:
    """Classify new reply emails for recent applications. Returns the new responses."""
    now = now or time.time()
    g = gmail.Gmail(client)
    since = now - settings.digest.response_window_days * 86400
    apps = conn.execute("SELECT * FROM applications WHERE status IN ('submitted','unconfirmed')"
                        " AND COALESCE(submitted_at, started_at) >= ?", (since,)).fetchall()
    new = []
    for a in apps:
        after = int(a["submitted_at"] or a["started_at"]) - 3600  # the confirmation can beat our own timestamp
        q = f"{_company_query(a['company'])} after:{after} {_ALERT_SENDERS}"
        for mid in await g.search(q, max_results=20):
            if conn.execute("SELECT 1 FROM responses WHERE message_id=?", (mid,)).fetchone():
                continue
            h = await g.headers(mid)
            kind = classify(h.get("subject", ""), h.get("snippet", ""))
            conn.execute("INSERT INTO responses (message_id, application_id, kind, subject, sender, received_at)"
                         " VALUES (?,?,?,?,?,?)", (mid, a["id"], kind, h.get("subject", "")[:300],
                                                   h.get("from", "")[:200], h.get("internal_date") or now))
            new.append({"application": dict(a), "kind": kind, "subject": h.get("subject", "")})
            current = conn.execute("SELECT outcome FROM applications WHERE id=?", (a["id"],)).fetchone()[0]
            if kind in RANK and RANK[kind] > RANK.get(current or "", 0) and current != "rejected":
                db.update_application(conn, a["id"], outcome=kind, outcome_at=h.get("internal_date") or now)
    return new


@dataclass
class Digest:
    title: str
    push: str
    markdown: str
    counts: dict = field(default_factory=dict)


def _line(r) -> str:
    return f"{r['company']}: {r['title']}"


def build(conn, settings: Settings, now: float | None = None, since: float | None = None) -> Digest:
    now = now or time.time()
    since = since or now - 86400
    applied = conn.execute("SELECT * FROM applications WHERE status='submitted' AND submitted_at >= ?"
                           " ORDER BY submitted_at", (since,)).fetchall()
    check = conn.execute("SELECT * FROM applications WHERE status IN ('unconfirmed','failed') AND started_at >= ?",
                         (since,)).fetchall()
    responses = conn.execute(
        "SELECT r.*, a.company, a.title FROM responses r JOIN applications a ON a.id = r.application_id"
        " WHERE r.received_at >= ? AND r.kind != 'other' ORDER BY r.received_at", (since,)).fetchall()
    ready = conn.execute(
        "SELECT j.*, p.needs_count, p.drafts FROM prepared p JOIN jobs j ON j.id = p.job_id"
        " WHERE p.error IS NULL AND j.closed_at IS NULL AND j.relevant=1 AND j.id NOT IN"
        " (SELECT job_id FROM applications WHERE status IN ('submitted','unconfirmed'))"
        " ORDER BY p.needs_count ASC, j.score DESC LIMIT 8").fetchall()
    found = conn.execute("SELECT COUNT(*) FROM jobs WHERE relevant=1 AND is_baseline=0 AND detected_at >= ?",
                         (since,)).fetchone()[0]
    dropped = conn.execute("SELECT COUNT(*) FROM jobs WHERE reason LIKE 'hard drop:%' AND detected_at >= ?",
                           (since,)).fetchone()[0]
    totals = dict(conn.execute("SELECT COALESCE(outcome, 'no reply yet'), COUNT(*) FROM applications"
                               " WHERE status='submitted' GROUP BY 1").fetchall())
    by_kind: dict[str, list] = {}
    for r in responses:
        by_kind.setdefault(r["kind"], []).append(r)

    counts = {"applied": len(applied), "check": len(check), "interviews": len(by_kind.get("interview", [])),
              "assessments": len(by_kind.get("assessment", [])), "rejections": len(by_kind.get("rejected", [])),
              "ready": sum(1 for r in ready if not r["needs_count"]), "found": found}
    bits = [f"{counts['applied']} applied"]
    if counts["interviews"]:
        bits.append(f"{counts['interviews']} interview{'s' * (counts['interviews'] > 1)}")
    if counts["assessments"]:
        bits.append(f"{counts['assessments']} assessment{'s' * (counts['assessments'] > 1)}")
    bits.append(f"{len(ready)} prepared")
    title = "ApplyPilot daily: " + " · ".join(bits)

    push = []
    for kind, label in (("interview", "Interview"), ("assessment", "Assessment"), ("rejected", "Rejected")):
        push += [f"{label}: {r['company']}" for r in by_kind.get(kind, [])]
    push += [f"Applied: {_line(r)}" for r in applied[:5]]
    push += [f"Check ({r['status']}): {r['company']}" for r in check[:3]]
    if ready:
        top = ready[0]
        need = "ready" if not top["needs_count"] else f"{top['needs_count']} for you"
        push.append(f"Next: {top['company']} ({need}) · run applypilot queue")
    push.append(f"New relevant jobs: {found} · hard drops: {dropped}")

    day = time.strftime("%A %b %d", time.localtime(now))
    md = [f"# ApplyPilot daily digest: {day}", ""]
    md += ["## Responses", *([f"- **{r['kind']}**: {r['company']}: {r['title']}: “{r['subject']}”"
                             for r in responses] or ["- none since the last digest"]), ""]
    md += ["## Applied", *([f"- {_line(r)} ({r['approved_via']}): {r['confirmation'] or ''}" for r in applied]
                           or ["- nothing in the last day"]), ""]
    if check:
        md += ["## Check these", *[f"- {r['status']}: {_line(r)}: {r['error'] or r['confirmation'] or ''}"
                                   for r in check], ""]
    md += ["## Ready to apply (from `applypilot queue`)"]
    for r in ready:
        k = len(json.loads(r["drafts"] or "[]"))
        need = "ready" if not r["needs_count"] else f"{r['needs_count']} for you"
        md.append(f"- {r['score']:.0f} · {_line(r)} · {need}" + (f" · {k} drafted" if k else ""))
    if not ready:
        md.append("- nothing prepared yet")
    md += ["", "## Pipeline (last day)", f"- new relevant jobs: {found}", f"- hard drops: {dropped}",
           "- all applications by outcome: " + (", ".join(f"{k} {v}" for k, v in totals.items()) or "none yet")]
    return Digest(title, "\n".join(push), "\n".join(md) + "\n", counts)


def _state_path():
    return home() / "digest_state.json"


def due(settings: Settings, now: float) -> bool:
    last = json.loads(_state_path().read_text()).get("date") if _state_path().exists() else None
    t = time.localtime(now)
    return settings.digest.enabled and t.tm_hour >= settings.digest.hour and last != time.strftime("%Y-%m-%d", t)


async def send(conn, settings: Settings, client: httpx.AsyncClient, now: float | None = None) -> Digest:
    now = now or time.time()
    last = json.loads(_state_path().read_text()).get("sent_at") if _state_path().exists() else None
    if gmail.token_info():
        try:
            await scan_responses(conn, settings, client, now)
        except gmail.GmailAuthError as e:
            log.warning("response scan skipped: %s", e)
    d = build(conn, settings, now, since=last)
    out = home() / "digests"
    out.mkdir(exist_ok=True)
    (out / f"{time.strftime('%Y-%m-%d', time.localtime(now))}.md").write_text(d.markdown)
    await notify.message(client, settings, d.title, d.push[:3500])
    _state_path().write_text(json.dumps({"date": time.strftime("%Y-%m-%d", time.localtime(now)), "sent_at": now}))
    return d


async def run_forever(conn, settings: Settings, client: httpx.AsyncClient) -> None:
    while True:
        try:
            if due(settings, time.time()):
                d = await send(conn, settings, client)
                log.info("digest sent: %s", d.title)
        except Exception as e:  # the digest is best-effort; it must never stop polling
            log.warning("digest failed: %r", e)
        await asyncio.sleep(600)
