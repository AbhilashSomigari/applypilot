import json
import time

import pytest
from fastapi.testclient import TestClient

from applypilot import db
from applypilot.config import get_settings, home
from applypilot.models import ATS, Job


@pytest.fixture
def web():
    get_settings.cache_clear()
    conn = db.connect(get_settings().db_path)
    now = time.time()
    jobs = [Job(ATS.GREENHOUSE, "acme", "1", "Software Engineer, New Grad", "Acme", "New York, NY",
                posted_at=now - 3600, url="https://job-boards.greenhouse.io/acme/jobs/1",
                description="We sponsor visas. Build APIs."),
            Job(ATS.ASHBY, "globex", "2", "ML Engineer", "Globex", "Remote", posted_at=now - 86400),
            Job("workday", "initech.wd5/Ext", "R3", "Data Engineer I", "Initech", "Austin, TX", posted_at=now - 7200)]
    db.insert_jobs(conn, jobs, now, False)
    for j, score, spons in zip(jobs, (82, 61, 70), ("offers", "no", "unknown"), strict=True):
        db.set_relevance(conn, j.id, True, "new-grad tech role, US")
        conn.execute("UPDATE jobs SET score=?, sponsorship=?, flags='[\"2+ years required\"]' WHERE id=?",
                     (score, spons, j.id))
    db.save_prepared(conn, jobs[0].id, now, needs=json.dumps([{"label": "Why Acme?", "note": "draft: read and edit",
                                                               "required": True}]), needs_count=0,
                     drafts=json.dumps([{"key": "q", "label": "Why Acme?", "text": "I build APIs.",
                                         "facts_used": [], "issues": []}]))
    app_id = db.add_application(conn, job_id=jobs[1].id, company="Globex", company_key="globex", title="ML Engineer",
                                ats="ashby", url="u", status="submitted", started_at=now - 600, submitted_at=now - 600,
                                confirmation="Thanks for applying", answers=json.dumps({"filled": [
                                    {"label": "Full Name", "key": "n", "wanted": "Jane Doe", "got": "Jane Doe",
                                     "status": "filled", "error": ""}]}), completed_by_user="[]")
    conn.execute("INSERT INTO responses VALUES ('m1', ?, 'interview', 'Next steps with Globex', 'r@globex.com', ?)",
                 (app_id, now - 60))
    db.update_application(conn, app_id, outcome="interview")
    from applypilot.web.app import create_app

    return TestClient(create_app()), app_id


def test_home_shows_counts_queue_and_replies(web):
    client, _ = web
    r = client.get("/")
    assert r.status_code == 200
    assert "Next to apply" in r.text and "Acme" in r.text and "ready · 1 drafted" in r.text
    assert "Next steps with Globex" in r.text
    assert "Initech" not in r.text  # Workday can't be filled yet, so it's not in the queue


def test_jobs_filters_and_partial(web):
    client, _ = web
    full = client.get("/jobs?ats=")
    assert full.status_code == 200 and "<form" in full.text and "Initech" in full.text
    assert "Globex" not in full.text  # applied jobs are hidden by default
    assert "Globex" in client.get("/jobs?ats=&show_applied=true").text
    part = client.get("/jobs?ats=&visa=sponsors", headers={"HX-Request": "true"})
    assert "<nav" not in part.text and "Acme" in part.text and "Initech" not in part.text
    assert "No jobs match" in client.get("/jobs?q=nothing-like-this").text


def test_job_page(web):
    client, _ = web
    r = client.get("/jobs/greenhouse:acme:1")
    assert r.status_code == 200 and "sponsors" in r.text and "2+ years required" in r.text
    assert "I build APIs." in r.text and "grounded" in r.text
    assert client.get("/jobs/greenhouse:nope:9").status_code == 404


def test_applications_and_detail(web):
    client, app_id = web
    r = client.get("/applications")
    assert "Globex" in r.text and "interview" in r.text
    d = client.get(f"/applications/{app_id}")
    assert "Thanks for applying" in d.text and "Next steps with Globex" in d.text and "Jane Doe" in d.text
    assert client.get("/applications/999").status_code == 404


def test_digest_page(web):
    client, _ = web
    r = client.get("/digest")
    assert r.status_code == 200 and "ApplyPilot daily" in r.text and "<h2>" in r.text


def test_files_only_serves_applypilot_screenshots(web):
    client, _ = web
    (home() / "applications").mkdir(exist_ok=True)
    (home() / "applications" / "a-filled.png").write_bytes(b"\x89PNG")
    (home() / "config.yaml").write_text("secret: x")
    assert client.get("/files/applications/a-filled.png").status_code == 200
    assert client.get("/files/applications/..%2Fconfig.yaml").status_code == 404
    assert client.get("/files/applications/config.yaml").status_code == 404
    assert client.get("/files/gmail_token/x.png").status_code == 404
