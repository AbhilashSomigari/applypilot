import time

import pytest
from test_apply_filler import CHROME

from applypilot import db
from applypilot.apply import submit
from applypilot.apply.answers import Answer
from applypilot.apply.forms import Field, Form
from applypilot.config import Settings
from applypilot.models import ATS, Job

FORM = """<html><body><form id="f" onsubmit="return go(event)">
<input type="text" name="name"><input type="email" name="email">
<label><input type="checkbox" name="agree"> I agree to the privacy notice</label>
<div id="err"></div><button type="submit">Submit application</button></form>
<script>
function go(e) {
  e.preventDefault();
  if (REQUIRE_AGREE && !document.querySelector('[name=agree]').checked) {
    document.getElementById('err').innerHTML = '<div role="alert">This field is required</div>'; return false; }
  location.href = 'thanks.html'; return false; }
</script></body></html>"""


def _setup(tmp_path, monkeypatch, conn, require_agree):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    (tmp_path / "form.html").write_text(FORM.replace("REQUIRE_AGREE", "true" if require_agree else "false"))
    (tmp_path / "thanks.html").write_text("<html><body><h1>Thank you for applying!</h1></body></html>")
    job = Job(ATS.LEVER, "acme", "1", "Software Engineer, New Grad", "Acme", "Remote", posted_at=time.time())
    db.insert_jobs(conn, [job], time.time(), False)
    form = Form("lever", (tmp_path / "form.html").as_uri(), [])
    plan = [Answer(Field("name", "Full name", "text", True), "Jane Doe", "profile"),
            Answer(Field("email", "Email", "email", True), "j@example.com", "profile"),
            Answer(Field("agree", "I agree to the privacy notice", "boolean", True), None, "needs_you",
                   "legal agreement / attestation: read and decide")]
    return db.get_job(conn, job.id), form, plan


def _asker(*replies):
    it = iter(replies)

    async def ask(_prompt):
        return next(it)

    return ask


@pytest.mark.browser
@pytest.mark.skipif(not CHROME, reason="needs a local Chrome")
async def test_submits_only_on_the_users_word_and_records_the_confirmation(tmp_path, monkeypatch, conn):
    row, form, plan = _setup(tmp_path, monkeypatch, conn, require_agree=False)
    said = []
    res = await submit.apply(conn, Settings(), row, form, plan, ask=_asker("maybe", "submit"), headless=True,
                             say=said.append)
    assert res["status"] == "submitted" and "thanks.html" in res["confirmation"]
    assert any("I agree to the privacy notice" in line for line in said)  # the legal notice was put to the user
    assert db.applied_to(conn, row["id"])["id"] == res["id"]
    assert (tmp_path / "applications").exists() and res["screenshot_after"].endswith("-after.png")


@pytest.mark.browser
@pytest.mark.skipif(not CHROME, reason="needs a local Chrome")
async def test_validation_errors_send_the_user_back_then_skip_abandons(tmp_path, monkeypatch, conn):
    row, form, plan = _setup(tmp_path, monkeypatch, conn, require_agree=True)
    said = []
    res = await submit.apply(conn, Settings(apply={"confirm_timeout_s": 5}), row, form, plan,
                             ask=_asker("submit", "skip"), headless=True, say=said.append)
    assert res["status"] == "abandoned" and db.applied_to(conn, row["id"]) is None
    assert any("This field is required" in line for line in said)


def test_per_company_cap(conn):
    s = Settings(apply={"max_per_company_30d": 2})
    now = time.time()
    for i in range(2):
        db.add_application(conn, job_id=f"j{i}", company="Acme, Inc.", company_key="acme", title="SWE", ats="lever",
                           url="u", status="submitted", started_at=now - 86400, submitted_at=now - 86400)
    db.add_application(conn, job_id="old", company="Acme", company_key="acme", title="SWE", ats="lever", url="u",
                       status="submitted", started_at=now - 40 * 86400, submitted_at=now - 40 * 86400)
    db.add_application(conn, job_id="x", company="Acme", company_key="acme", title="SWE", ats="lever", url="u",
                       status="abandoned", started_at=now)
    assert submit.cap_reason(conn, s, "ACME") == "already 2 applications to ACME in 30 days (cap 2)"
    assert submit.cap_reason(conn, s, "Globex") is None
