import json
import time

import pytest
import respx
from test_apply_filler import CHROME
from test_apply_submit import _setup

from applypilot import db, notify
from applypilot.apply import approval, submit
from applypilot.apply.answers import Answer
from applypilot.apply.forms import Field
from applypilot.config import Settings
from applypilot.models import ATS, Job

ON = {"enabled": True, "topic": "ok-topic", "secret": "s3cret", "min_supervised": 1}


def S(**approval_cfg):
    return Settings(ntfy_topic="main-topic", mac_notifications=False, approval={**ON, **approval_cfg})


def _supervise(conn, n=1, via="mac"):
    for i in range(n):
        db.add_application(conn, job_id=f"done{i}", company="X", company_key="x", title="t", ats="lever", url="u",
                           status="submitted", started_at=time.time(), approved_via=via)


def _ready_job(conn, needs=(), drafts=(), captcha=None):
    j = Job(ATS.GREENHOUSE, "acme", "7", "Software Engineer, New Grad", "Acme", "Remote", posted_at=time.time(),
            url="https://job-boards.greenhouse.io/acme/jobs/7", apply_url="https://job-boards.greenhouse.io/acme/jobs/7")
    db.insert_jobs(conn, [j], time.time(), False)
    conn.execute("UPDATE jobs SET score=80, variant='swe-backend' WHERE id=?", (j.id,))
    db.save_prepared(conn, j.id, time.time(), needs=json.dumps(list(needs)), needs_count=len(needs),
                     drafts=json.dumps(list(drafts)), captcha=captcha)
    return db.get_job(conn, j.id), db.get_prepared(conn, j.id)


def test_tokens_are_bound_to_the_job_and_expire():
    t = approval.sign("k", "greenhouse:acme:7", time.time(), 24)
    assert approval.verify("k", "greenhouse:acme:7", t, time.time())
    assert not approval.verify("k", "greenhouse:acme:8", t, time.time())
    assert not approval.verify("other", "greenhouse:acme:7", t, time.time())
    assert not approval.verify("k", "greenhouse:acme:7", t[:-1] + "0", time.time())
    assert not approval.verify("k", "greenhouse:acme:7", t, time.time() + 25 * 3600)
    assert not approval.verify("k", "greenhouse:acme:7", "garbage", time.time())


def test_only_forms_needing_nothing_are_eligible(conn):
    assert approval.eligible(_ready_job(conn)[1]) == (True, "ready")
    conn.execute("DELETE FROM jobs"), conn.execute("DELETE FROM prepared")
    legal = [{"label": "Agreement to Arbitrate", "note": "legal", "required": True}]
    assert approval.eligible(_ready_job(conn, needs=legal)[1])[0] is False
    conn.execute("DELETE FROM jobs"), conn.execute("DELETE FROM prepared")
    assert approval.eligible(_ready_job(conn, drafts=[{"key": "q"}])[1])[0] is False
    conn.execute("DELETE FROM jobs"), conn.execute("DELETE FROM prepared")
    assert approval.eligible(_ready_job(conn, captcha="hcaptcha")[1])[0] is False


def test_phone_approval_waits_for_supervised_applications(conn):
    assert approval.active(conn, Settings(approval={**ON, "enabled": False}))[0] is False
    s = S(min_supervised=2)
    _supervise(conn, 1)
    _supervise(conn, 1, via="phone")  # phone-approved ones don't count toward supervision
    on, why = approval.active(conn, s)
    assert not on and why == "1 of 2 supervised applications so far"
    _supervise(conn, 1)
    assert approval.active(conn, s) == (True, "on")


async def test_offer_push_carries_a_signed_apply_button_once(conn, client):
    _supervise(conn)
    row, prep = _ready_job(conn)
    with respx.mock:
        route = respx.post("https://ntfy.sh").respond(200)
        assert await approval.offer(conn, S(), client, row, prep)
        assert not await approval.offer(conn, S(), client, row, db.get_prepared(conn, row["id"]))  # once
    body = json.loads(route.calls[0].request.content)
    apply_btn = body["actions"][0]
    assert body["topic"] == "main-topic" and apply_btn["url"] == "https://ntfy.sh/ok-topic"
    job_id, token = apply_btn["body"].split()
    assert job_id == row["id"] and approval.verify("s3cret", job_id, token, time.time())


async def test_messages_are_checked_once_and_refused_without_a_valid_token(conn, client):
    _supervise(conn)
    calls = []

    async def apply_fn(job_id):
        calls.append(job_id)
        return "submitted"

    good = approval.sign("s3cret", "j1", time.time(), 24)
    assert await approval.handle(conn, S(), client, "m1", "j1", "1.bad", apply_fn) == "refused: bad or expired token"
    assert await approval.handle(conn, S(), client, "m2", "j1", good, apply_fn) == "submitted"
    assert await approval.handle(conn, S(), client, "m2", "j1", good, apply_fn) == "duplicate"
    off = Settings(approval={**ON, "min_supervised": 99})
    assert (await approval.handle(conn, off, client, "m3", "j1", good, apply_fn)).startswith("refused: 1 of 99")
    assert calls == ["j1"]
    assert approval.parse('{"event":"message","id":"m9","message":"j1 tok"}') == ("m9", "j1", "tok")
    assert approval.parse('{"event":"keepalive","id":"k"}') is None


async def test_a_form_that_changed_is_refused_and_the_user_is_told(conn, client, monkeypatch):
    sent = []

    async def fake_message(_c, _s, title, body, priority="default"):
        sent.append(title)

    monkeypatch.setattr(notify, "message", fake_message)
    row, _ = _ready_job(conn)
    with respx.mock:
        respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").respond(200, json={"questions": [
            {"label": "Agreement to Arbitrate", "required": True, "fields": [
                {"name": "question_1", "type": "multi_value_single_select", "values": [{"label": "I agree",
                                                                                         "value": 1}]}]}]})
        res = await approval.apply_job(conn, S(), client, row["id"])
    assert res.startswith("refused: 1 question(s) now need you") and sent == ["Needs you at the Mac: Acme"]
    db.add_application(conn, job_id=row["id"], company="Acme", company_key="acme", title="t", ats="greenhouse",
                       url="u", status="submitted", started_at=time.time())
    assert await approval.apply_job(conn, S(), client, row["id"]) == "refused: already applied"


@pytest.mark.browser
@pytest.mark.skipif(not CHROME, reason="needs a local Chrome")
async def test_unattended_apply_never_submits_when_a_field_does_not_verify(tmp_path, monkeypatch, conn):
    row, form, plan = _setup(tmp_path, monkeypatch, conn, require_agree=False)
    plan = [a for a in plan if a.source != "needs_you"]
    (tmp_path / "r.pdf").write_text("%PDF")
    plan.append(Answer(Field("resume_missing", "Resume", "file", False), str(tmp_path / "r.pdf"), "resume"))
    bad = plan + [Answer(Field("nope", "Sponsorship?", "select", True, ["Yes", "No"]), "Yes", "answers")]
    res = await submit.apply_unattended(conn, Settings(), row, form, bad, headless=True)
    assert res["status"] == "failed" and "didn't verify" in res["error"] and res["approved_via"] == "phone"
    assert db.applied_to(conn, row["id"]) is None
    ok = await submit.apply_unattended(conn, Settings(), row, form, plan, headless=True)
    assert ok["status"] == "submitted" and "thanks.html" in ok["confirmation"]
