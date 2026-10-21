"""Screen a relevant job (description filters) and give it a 0–100 priority score.

The score orders the queue; it never drops anything (only `filters.assess` hard drops do).
Weights, in points:
- freshness (0–35): full within the first hours, fading to 0 at two weeks; being first matters most;
- new-grad title (15);
- visa outlook (4–20): says it sponsors 20 · H-1B history ≥ 100 approvals 18, ≥ 25 14, any 10 ·
  unknown 8 · says no sponsorship 4 (still the OPT/STEM OPT path);
- E-Verify (10): needed for the STEM OPT extension;
- apply path (0–10): a polled ATS 10, another ATS 5, an aggregator link 0;
- knockouts: 3 years required −10, 2 years −4, PhD required −15, C2C/1099 −10, a "now or in the future"
  sponsorship question −3, clearance or citizenship listed as preferred −5.
Fit against the resume variants is added in Phase 3b.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time

from applypilot import db, resolver
from applypilot.filters import Assessment, assess, label
from applypilot.models import Job
from applypilot.relevance import NEW_GRAD


def score(job: Job, a: Assessment, h1b: int | None, now: float | None = None) -> float:
    now = now or time.time()
    s = 0.0
    if job.posted_at:
        age_h = max(0.0, (now - job.posted_at) / 3600)
        s += 35 * max(0.0, 1 - math.log1p(age_h) / math.log1p(14 * 24))
    else:
        s += 10
    s += 15 if NEW_GRAD.search(job.title) else 0
    if a.sponsorship == "offers":
        s += 20
    elif h1b and h1b >= 100:
        s += 18
    elif h1b and h1b >= 25:
        s += 14
    elif h1b:
        s += 10
    elif a.sponsorship == "no":
        s += 4
    else:
        s += 8
    s += 10 if a.everify else 0
    s += 10 if job.ats in resolver.POLLABLE else (0 if resolver.is_aggregator(job.url or "") else 5)
    if a.years == 3:
        s -= 10
    elif a.years == 2:
        s -= 4
    s -= 15 if "PhD required" in a.flags else 0
    s -= 10 if "contract (C2C/1099)" in a.flags else 0
    s -= 3 if a.future_question else 0
    s -= 5 if {"clearance preferred", "citizenship preferred"} & set(a.flags) else 0
    return round(max(0.0, min(100.0, s)), 1)


def h1b_for(conn: sqlite3.Connection, job: Job) -> int | None:
    row = conn.execute("SELECT h1b_approvals FROM boards WHERE ats=? AND slug=?", (str(job.ats), job.slug)).fetchone()
    if row and row[0]:
        return row[0]
    if job.ats == "other" and job.company:
        return max((b["h1b_approvals"] or 0 for b in resolver.boards_for_company(conn, job.company)), default=0) or None
    return None


def screen(conn: sqlite3.Connection, job: Job, now: float | None = None) -> tuple[bool, str]:
    """Assess a relevant job's description and store the result. Returns (keep, visa/score label).
    A hard drop marks the job not relevant, so it is never pushed or queued."""
    a = assess(job.description)
    h1b = h1b_for(conn, job)
    s = score(job, a, h1b, now)
    db.set_screen(conn, job.id, s, a.sponsorship, a.everify, json.dumps(a.flags), now or time.time())
    if a.drop:
        db.set_relevance(conn, job.id, False, f"hard drop: {a.drop}")
        return False, ""
    text = label(a, h1b)
    return True, f"score {s:.0f}" + (f" · {text}" if text else "")
