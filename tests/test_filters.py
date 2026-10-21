import json
import time

import pytest
import respx
from conftest import FIX
from test_poller import GH, force_due, gh_job

from applypilot import db, notify, rank
from applypilot.filters import assess
from applypilot.models import ATS, Job
from applypilot.poller import Poller

CATEGORY = {"requires U.S. citizenship or green card": "citizenship", "requires security clearance": "clearance",
            "U.S. persons only (ITAR / export control)": "us_person", "OPT training / bench-sales scheme": "scam"}


def _cat(drop):
    return CATEGORY.get(drop, "years" if drop else None)


def test_labeled_eval_set():
    """73 hand-labeled excerpts (70 live postings, 3 synthetic): every hard drop caught, nothing else dropped.
    The plan's bar: 100% recall on hard drops and no OPT-eligible ("no sponsorship") job dropped."""
    rows = [json.loads(line) for line in (FIX / "jd_eval.jsonl").read_text().splitlines()]
    missed, false_drops, wrong_cat = [], [], []
    for r in rows:
        cat = _cat(assess(r["text"]).drop)
        if r["drop"] and not cat:
            missed.append(r["company"])
        elif not r["drop"] and cat:
            false_drops.append((r["company"], cat))
        elif cat and cat not in r["drop"]:
            wrong_cat.append((r["company"], cat))
    assert len(rows) == 73 and sum(not r["drop"] and r["sponsorship"] == "no" for r in rows) == 12
    assert missed == [] and false_drops == [] and wrong_cat == []


@pytest.mark.parametrize("text, drop", [
    # equal-opportunity boilerplate is not a requirement
    ("We do not discriminate on the basis of race, citizenship, age or any other protected characteristic.", None),
    # "U.S. Citizen" must not be split at "U.S."
    ("Requirements:\n- Must be a U.S. Citizen.\n- 1+ years of Python", "citizenship"),
    # a softener in parentheses only softens the aside
    ("Required\n- U.S. citizenship and eligibility for a Top Secret clearance (active TS strongly preferred)",
     "clearance"),
    ("Nice to have:\n- Active or previous security clearance", None),
    ("Active US Security clearance, or eligibility to obtain one, is beneficial but not necessary.", None),
    ("Clearance:\nNot required", None),
    ("Clearance Level: Top Secret / SCI, Must Have Clearance to Start", "clearance"),
    ("Lead end-to-end validation and clearance process for safety-critical monitors.", None),
    # a legal requirement outweighs an inherited Preferred section
    ("Preferred Qualifications\n- Go experience\nUnder ITAR you must be a U.S. citizen or lawful permanent resident.",
     "citizenship"),
    ("This role requires access to export-controlled items that require U.S. Person status.", "us_person"),
    ("We are proud of our compliance with SOC2, FedRAMP, or ITAR.", None),
    # clauses that admit visa holders
    ("U.S. Citizens, Green Card Holders, EAD Holders, and H-1B transfer candidates are encouraged to apply.", None),
    ("We will consider non-US citizens and will provide green card sponsorship.", None),
    # experience: curly-apostrophe headers, alternatives, noise
    ("WHAT WE’RE LOOKING FOR:\n- 5+ years of experience in fraud or risk", "years"),
    ("Required Qualifications: Bachelor's degree plus 2 years of experience OR Master's degree with 0 years of "
     "experience.", None),
    ("Qualifications\n- 2-4 years of professional software engineering experience", None),
    ("The Deep Breath: every 5 years, take 12 weeks of paid leave. Clay spent 18 years at Google.", None),
    # scams vs legitimate OPT wording
    ("Stop the OPT clock: enroll in our 6-week Java training program. Guaranteed project allotment.", "scam"),
    ("Candidates on F-1 OPT or the STEM OPT program are welcome.", None),
    ("Experience with model evals or post-training feedback loops.", None),  # "training fee" inside a word
])
def test_clauses(text, drop):
    assert _cat(assess(text).drop) == drop


def test_sponsorship_is_a_signal_never_a_drop():
    for text in ("We are unable to provide visa sponsorship now or in the future.",
                 "Must be authorized to work in the U.S. without the need for current or future employer sponsorship.",
                 "**No Sponsorship offered for this position**"):
        a = assess(text)
        assert a.drop is None and a.sponsorship == "no"
    assert assess("Visa sponsorship: Yes, we sponsor visas.").sponsorship == "offers"
    a = assess("Applicants must be authorized to work without sponsorship, now or in the future. We use E-Verify.")
    assert a.future_question and a.everify


def _job(title="Software Engineer, New Grad", age_h=1.0, ats=ATS.ASHBY, desc=""):
    return Job(ats, "acme", "1", title, "Acme", "New York, NY", posted_at=time.time() - age_h * 3600,
               url="https://jobs.ashbyhq.com/acme/1", description=desc)


def test_score_orders_fresh_sponsoring_new_grad_first():
    fresh = rank.score(_job(), assess("We sponsor visas. We participate in E-Verify."), None)
    old = rank.score(_job("Software Engineer", age_h=24 * 10), assess(""), None)
    no_sponsor = rank.score(_job(), assess("We are unable to sponsor visas."), None)
    knockout = rank.score(_job(), assess("Requirements:\n- 3+ years of experience\n- PhD required"), None)
    assert fresh > no_sponsor > knockout and fresh > old and no_sponsor > 0
    assert rank.score(_job(), assess(""), h1b=250) > rank.score(_job(), assess(""), h1b=None)


def test_screen_stores_signals_and_hard_drops(conn):
    job = _job(desc="Requirements:\n- Must be a U.S. citizen")
    db.insert_jobs(conn, [job], time.time(), False)
    db.set_relevance(conn, job.id, True, "new-grad tech role, US")
    assert rank.screen(conn, job) == (False, "")
    row = db.get_job(conn, job.id)
    assert row["relevant"] == 0 and row["reason"] == "hard drop: requires U.S. citizenship or green card"
    ok = _job(desc="We are unable to sponsor visas.")
    ok.ext_id = "2"
    db.insert_jobs(conn, [ok], time.time(), False)
    keep, signals = rank.screen(conn, ok)
    assert keep and "no sponsorship: OPT/STEM OPT path" in signals
    assert db.get_job(conn, ok.id)["sponsorship"] == "no"


async def test_poller_never_pushes_a_hard_drop(conn, settings, client, monkeypatch):
    pushed = []

    async def fake_push(_c, _s, job, reason):
        pushed.append(job.title)

    monkeypatch.setattr(notify, "push", fake_push)
    db.add_board(conn, "greenhouse", "acme", "Acme", "warm")
    p = Poller(conn, settings, client)
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1)]})
        await p.run_once()  # baseline
    force_due(conn)
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1), gh_job(2, "ML Engineer"), gh_job(3, "Data Engineer I")]})
        respx.get(f"{GH}/2").respond(200, json={"content": "&lt;p&gt;Active TS/SCI clearance required.&lt;/p&gt;"})
        respx.get(f"{GH}/3").respond(200, json={"content": "&lt;p&gt;We cannot sponsor visas.&lt;/p&gt;"})
        s = await p.run_once()
    assert pushed == ["Data Engineer I"] and [j.title for j, _ in s.relevant] == ["Data Engineer I"]
    assert db.get_job(conn, "greenhouse:acme:2")["reason"] == "hard drop: requires security clearance"
