import json
import time

import pytest
import respx

from applypilot import db, digest, gmail, notify
from applypilot.config import Settings
from applypilot.models import ATS, Job


@pytest.mark.parametrize("subject, snippet, kind", [
    ("Thank you for applying to Acme", "We've received your application for Software Engineer", "received"),
    ("Your application to Acme", "Unfortunately, we have decided to move forward with other candidates", "rejected"),
    ("Next steps with Acme", "We'd love to schedule a call to discuss your availability", "interview"),
    ("Acme coding challenge", "Please complete the HackerRank assessment within 7 days", "assessment"),
    # a rejection that mentions an interview is still a rejection
    ("Update on your interview", "Unfortunately we will not be moving forward", "rejected"),
    ("Acme newsletter", "Read our latest blog post", "other"),
])
def test_classify(subject, snippet, kind):
    assert digest.classify(subject, snippet) == kind


def _app(conn, company="Acme", hours_ago=30, status="submitted", **kw):
    t = time.time() - hours_ago * 3600
    return db.add_application(conn, job_id=f"j-{company}", company=company, company_key=company.lower(),
                              title="Software Engineer", ats="greenhouse", url="u", status=status, started_at=t,
                              submitted_at=t if status == "submitted" else None, confirmation="thanks", **kw)


def _signed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    gmail._write_secret(gmail.token_file(), {"refresh_token": "rt", "access_token": "at",
                                             "expires_at": time.time() + 600, "obtained_at": time.time()})


async def test_responses_move_the_outcome_forward_and_are_classified_once(tmp_path, monkeypatch, conn, client):
    _signed_in(tmp_path, monkeypatch)
    app_id = _app(conn)
    msgs = {"m1": ("Thank you for applying to Acme", "We received your application", 1),
            "m2": ("Next steps", "We'd like to schedule a call to discuss your availability", 2)}
    with respx.mock:
        search = respx.get(f"{gmail.API}/messages").respond(200, json={"messages": [{"id": m} for m in msgs]})
        for mid, (subj, snip, d) in msgs.items():
            respx.get(f"{gmail.API}/messages/{mid}").respond(200, json={
                "internalDate": str(int((time.time() - 3600 * d) * 1000)), "snippet": snip,
                "payload": {"headers": [{"name": "Subject", "value": subj}, {"name": "From", "value": "r@acme.com"}]}})
        new = await digest.scan_responses(conn, Settings(), client)
        again = await digest.scan_responses(conn, Settings(), client)
    q = search.calls[0].request.url.params["q"]
    assert '"Acme"' in q and "after:" in q and "-from:(indeed.com" in q
    assert [n["kind"] for n in new] == ["received", "interview"] and again == []
    assert conn.execute("SELECT outcome FROM applications WHERE id=?", (app_id,)).fetchone()[0] == "interview"


async def test_a_rejection_is_final(tmp_path, monkeypatch, conn, client):
    _signed_in(tmp_path, monkeypatch)
    app_id = _app(conn)
    db.update_application(conn, app_id, outcome="rejected", outcome_at=time.time())
    with respx.mock:
        respx.get(f"{gmail.API}/messages").respond(200, json={"messages": [{"id": "m3"}]})
        respx.get(f"{gmail.API}/messages/m3").respond(200, json={"internalDate": str(int(time.time() * 1000)),
                                                                "snippet": "Thank you for applying",
                                                                "payload": {"headers": []}})
        await digest.scan_responses(conn, Settings(), client)
    assert conn.execute("SELECT outcome FROM applications WHERE id=?", (app_id,)).fetchone()[0] == "rejected"


def test_build_summarizes_the_day(conn):
    now = time.time()
    a = _app(conn, "Acme", hours_ago=5, approved_via="phone")
    _app(conn, "Globex", hours_ago=3, status="unconfirmed")
    conn.execute("INSERT INTO responses VALUES ('m1', ?, 'interview', 'Next steps', 'r@acme.com', ?)", (a, now - 60))
    j = Job(ATS.ASHBY, "initech", "1", "ML Engineer, New Grad", "Initech", "Remote", posted_at=now)
    db.insert_jobs(conn, [j], now, False)
    db.set_relevance(conn, j.id, True, "new-grad tech role, US")
    conn.execute("UPDATE jobs SET score=81 WHERE id=?", (j.id,))
    db.save_prepared(conn, j.id, now, needs="[]", needs_count=0, drafts="[]")
    d = digest.build(conn, Settings(), now)
    assert d.title == "ApplyPilot daily: 1 applied · 1 interview · 1 prepared"
    assert "Interview: Acme" in d.push and "Check (unconfirmed): Globex" in d.push
    assert "Next: Initech (ready)" in d.push
    assert "## Responses" in d.markdown and "**interview**: Acme" in d.markdown and "(phone)" in d.markdown


def test_due_once_a_day_after_the_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    s = Settings(digest={"hour": 8})
    morning = time.mktime((2026, 10, 22, 7, 30, 0, 0, 0, -1))
    later = time.mktime((2026, 10, 22, 9, 0, 0, 0, 0, -1))
    assert not digest.due(s, morning) and digest.due(s, later)
    (tmp_path / "digest_state.json").write_text(json.dumps({"date": "2026-10-22", "sent_at": later}))
    assert not digest.due(s, later + 3600)
    assert digest.due(s, later + 86400)


async def test_send_pushes_saves_and_records(tmp_path, monkeypatch, conn, client):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    sent = []

    async def fake_message(_c, _s, title, body, priority="default"):
        sent.append((title, body))

    monkeypatch.setattr(notify, "message", fake_message)
    d = await digest.send(conn, Settings(), client)  # not signed in to Gmail: the response scan is skipped
    assert sent == [(d.title, d.push)]
    assert list((tmp_path / "digests").glob("*.md")) and (tmp_path / "digest_state.json").exists()
