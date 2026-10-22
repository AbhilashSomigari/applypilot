import json
import sys
import time

import pytest
import respx
from conftest import FIX

from applypilot import db
from applypilot.apply import answers, prepare
from applypilot.apply.answers import Answer
from applypilot.apply.forms import Field
from applypilot.config import Settings
from applypilot.models import ATS, Job

GQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
FAKE = """
import json, sys
print(json.dumps({"structured_output": {"answers": [
    {"key": "1821fd3c-40e5-488b-afd7-a03c5454d198", "draft": "I built OpsPilot to 93% accuracy.",
     "facts_used": ["93% accuracy"]}]}}))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("## Verified career facts\n- OpsPilot reached **93% accuracy**\n")
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE}")
    script.chmod(0o755)
    monkeypatch.setattr(answers, "load_user", lambda: ({"name": {"first": "Jane", "last": "Doe"}}, {}))
    return Settings(ntfy_topic="", mac_notifications=False,
                    tailor={"resume_dir": str(tmp_path), "claude_bin": str(script)})


def _job(conn, ext="cf34f80e-fe5c-454d-bc9a-4c59993ffda0", score=80.0, age_d=1.0, ats=ATS.ASHBY):
    j = Job(ats, "quora", ext, "Software Engineer New Grad", "Quora", "Remote", posted_at=time.time() - age_d * 86400,
            apply_url=f"https://jobs.ashbyhq.com/quora/{ext}/application", description="Build ML platform.")
    db.insert_jobs(conn, [j], time.time(), False)
    db.set_relevance(conn, j.id, True, "new-grad tech role, US")
    conn.execute("UPDATE jobs SET score=? WHERE id=?", (score, j.id))
    return j.id


def test_candidates_are_fresh_high_scoring_unapplied_ats_jobs(conn, env):
    keep = _job(conn)
    _job(conn, "low", score=40)
    _job(conn, "old", age_d=10)
    applied = _job(conn, "done")
    db.add_application(conn, job_id=applied, company="Quora", company_key="quora", title="SWE", ats="ashby",
                       url="u", status="submitted", started_at=time.time())
    assert [r["id"] for r in prepare.candidates(conn, env, 10)] == [keep]


async def test_prepare_reads_the_form_drafts_and_stores_what_needs_the_user(conn, env, client):
    jid = _job(conn)
    with respx.mock:
        respx.post(GQL).respond(200, json=json.loads((FIX / "ashby_form_quora.json").read_text()))
        n = await prepare.run_once(conn, env, client)
    p = db.get_prepared(conn, jid)
    assert n == 1 and p["error"] is None and p["drafted"] == 1
    [d] = json.loads(p["drafts"])
    assert d["text"] == "I built OpsPilot to 93% accuracy." and d["issues"] == []
    needs = json.loads(p["needs"])
    assert any(x["label"].startswith("I understand") for x in needs)  # the attestation stays the user's
    assert p["needs_count"] == sum(1 for x in needs if not x["note"].startswith("draft"))
    assert prepare.candidates(conn, env, 10) == []  # prepared once, not again


async def test_unreadable_form_is_recorded_and_retried_after_a_day(conn, env, client):
    jid = _job(conn)
    with respx.mock:
        respx.post(GQL).respond(500)
        await prepare.run_once(conn, env, client)
    assert db.get_prepared(conn, jid)["error"].startswith("HTTPStatusError")
    assert prepare.candidates(conn, env, 10) == []
    conn.execute("UPDATE prepared SET prepared_at = prepared_at - 2*86400")
    assert [r["id"] for r in prepare.candidates(conn, env, 10)] == [jid]


async def test_daily_drafting_cap(conn, env, client):
    env.prepare.max_drafted_per_day = 0
    jid = _job(conn)
    with respx.mock:
        respx.post(GQL).respond(200, json=json.loads((FIX / "ashby_form_quora.json").read_text()))
        await prepare.run_once(conn, env, client)
    p = db.get_prepared(conn, jid)
    assert p["drafted"] == 0 and json.loads(p["drafts"]) == []


def test_stored_drafts_are_reused_only_for_unchanged_wording(conn):
    db.save_prepared(conn, "j1", time.time(), drafts=json.dumps([
        {"key": "q1", "label": "Why Acme?", "text": "Because.", "facts_used": [], "issues": []},
        {"key": "q2", "label": "Describe a project", "text": "OpsPilot.", "facts_used": [], "issues": []}]))
    plan = [Answer(Field("q1", "Why Acme?", "textarea", True), None, "needs_you", "written answer (essay)"),
            Answer(Field("q2", "Describe a project you led", "textarea", True), None, "needs_you", "x")]
    assert [d.key for d in prepare.stored_drafts(conn, "j1", plan)] == ["q1"]
