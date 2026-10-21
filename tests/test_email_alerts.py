import base64
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

import respx

from applypilot import db, gmail, notify
from applypilot.config import Settings
from applypilot.feeds import FeedRunner
from applypilot.feeds.email_alerts import EmailAlerts, parse_indeed
from applypilot.models import ATS, Job

JK = "0123456789abcdef"
# shaped like real Indeed emails (2026-10); companies and links are made up
DIGEST = f"""Indeed Job Alert
3 new ai/ml engineer jobs (Remote)

Jobs 1-3 of 3 new jobs
See matching results on Indeed: https://www.indeed.com/jobs?q=ai&tmtk=x

Backend Engineer, New Grad
Acme Robotics - Remote
$90,000 - $120,000 a year
Responsive employer
Easily apply
We are hiring a backend engineer to build our robot fleet APIs, work with Python and Postgres…
Just posted
https://www.indeed.com/rc/clk/dl?jk={JK}&tk=t1&from=ja&alid=a1

ML Engineer
Globex - Pittsburgh, PA
Build ranking models for search…
3 days ago
https://www.indeed.com/pagead/clk/dl?mo=r&ad=-6NYlbfk&camk=x

Salaries estimated if unavailable.
"""
MATCHES = """Hi there,  Your background could be a great match for this role. Interested? Apply now.
Jobs are based on your preferences, profile, and activity on Indeed &sup1;

Data Analyst - AI Trainer
Initech - Lubbock, TX
$40 an hour
Easily apply
Work with state-of-the-art AI models…

https://www.indeed.com/pagead/clk/dl?mo=r&ad=abc
"""
SINGLE = """Hi there,

Your background in machine learning could be a strong match for this role at Hooli.

Machine Learning Engineer
Hooli
Remote
Salary: $135,000 a year
Job type: Full-time

View job: https://cts.indeed.com/v3/H4sIAAAAopaque
Apply now: https://cts.indeed.com/v3/H4sIAAAAopaque2
"""
ACTIVE = """Your job alert is active

You'll receive your first daily job alert when jobs become available.

In the meantime, you can browse new jobs on Indeed any time by copying and pasting the link below

https://engage.indeed.com/f/a/xyz~~/AAR9h
"""


def mail(body, subject="3 new jobs", sent=None, sender="donotreply@jobalert.indeed.com"):
    m = EmailMessage()
    m["From"], m["Subject"] = f"Indeed <{sender}>", subject
    m["Date"] = format_datetime(datetime.fromtimestamp(sent or time.time(), UTC))
    m.set_content(body)
    m.add_alternative("<p>html part</p>", subtype="html")
    return m


def test_digest_cards():
    sent = time.time() - 600
    a, b = parse_indeed("m1", mail(DIGEST, sent=sent))
    assert (a.title, a.company, a.location) == ("Backend Engineer, New Grad", "Acme Robotics", "Remote")
    assert a.urls == [f"https://www.indeed.com/viewjob?jk={JK}"] and a.item_id == "m1:0"
    assert a.raw["tags"] == ["Responsive employer", "Easily apply"] and a.raw["salary"].startswith("$90,000")
    assert abs(a.posted_at - sent) < 2 and a.description.startswith("We are hiring")
    # sponsored cards carry no job key: a stable search link instead of the per-email tracker
    assert b.location == "Pittsburgh, PA" and b.raw["sponsored"] and abs(b.posted_at - (sent - 3 * 86400)) < 2
    assert b.urls[0].startswith("https://www.indeed.com/jobs?q=%22ML+Engineer%22+Globex")


def test_match_and_single_job_formats():
    (c,) = parse_indeed("m2", mail(MATCHES, sender="donotreply@match.indeed.com"))
    assert (c.title, c.company, c.location) == ("Data Analyst - AI Trainer", "Initech", "Lubbock, TX")
    (s,) = parse_indeed("m3", mail(SINGLE, subject="Machine Learning Engineer @ Hooli"))
    assert (s.title, s.company, s.location, s.raw["salary"]) == ("Machine Learning Engineer", "Hooli", "Remote",
                                                                  "$135,000 a year")
    assert parse_indeed("m4", mail(ACTIVE, subject="Your job alert is now active")) == []


def _api(msgs):
    """Gmail API mocks: messages.list returns msgs (id -> EmailMessage), messages.get the raw bytes."""
    lst = respx.get(f"{gmail.API}/messages").respond(200, json={"messages": [{"id": i} for i in msgs]})
    for i, m in msgs.items():
        respx.get(f"{gmail.API}/messages/{i}").respond(200, json={
            "raw": base64.urlsafe_b64encode(m.as_bytes()).decode(), "internalDate": str(int(time.time() * 1000))})
    respx.get(url__regex=r"https://(boards-api\.greenhouse\.io|api\.lever\.co|api\.ashbyhq\.com)/.*").respond(404)
    return lst


def _sign_in(obtained_at=None):
    gmail._write_secret(gmail.token_file(), {"refresh_token": "rt", "access_token": "at",
                                             "expires_at": time.time() + 600,
                                             "obtained_at": obtained_at or time.time(), "email": "me@gmail.com"})


async def test_indeed_alert_resolves_to_the_ats_job_and_pushes_once(ap_home, conn, settings, client, monkeypatch):
    pushed = []

    async def fake_push(_c, _s, job, reason):
        pushed.append((job.id, reason))

    monkeypatch.setattr(notify, "push", fake_push)
    _sign_in()
    # Acme Robotics is tracked; its board baseline already holds the job the alert names
    db.add_board(conn, "greenhouse", "acmerobotics", "Acme Robotics", "warm")
    conn.execute("UPDATE boards SET baseline_done=1")
    db.insert_jobs(conn, [Job(ATS.GREENHOUSE, "acmerobotics", "7", "Backend Engineer - New Grad", "Acme Robotics",
                              "Remote, US", posted_at=time.time() - 3600,
                              apply_url="https://job-boards.greenhouse.io/acmerobotics/jobs/7")], time.time(), True)
    db.set_relevance(conn, "greenhouse:acmerobotics:7", True, "new-grad tech role, US")
    feed = EmailAlerts("indeed", settings)
    conn.execute("INSERT INTO feeds (name, baseline_done) VALUES (?, 1)", (feed.name,))
    r = FeedRunner(conn, settings, client, feeds=[feed])
    with respx.mock:
        lst = _api({"m1": mail(DIGEST)})
        s = await r.run_once()
    assert "after:" in lst.calls[0].request.url.params["q"]
    assert s.leads == 2 and s.resolved_by_name == 1 and s.new_jobs == 1
    # the ATS job is pushed with its employer apply link; Globex (untracked) keeps a stable Indeed link
    assert pushed[0] == ("greenhouse:acmerobotics:7", "new-grad tech role, US · via Indeed")
    other = conn.execute("SELECT url, source FROM jobs WHERE ats='other'").fetchone()
    assert other["url"].startswith("https://www.indeed.com/jobs?q=") and other["source"] == "gmail:indeed"
    assert len(pushed) == 2

    # the next poll sees the same message again (overlap window): not re-parsed, nothing pushed
    conn.execute("UPDATE feeds SET next_poll=0")
    with respx.mock:
        _api({"m1": mail(DIGEST)})
        s2 = await r.run_once()
    assert s2.leads == 0 and len(pushed) == 2


async def test_expired_sign_in_pushes_one_notice(ap_home, conn, client, monkeypatch):
    notices = []

    async def fake_message(_c, _s, title, body, priority="default"):
        notices.append(title)

    monkeypatch.setattr(notify, "message", fake_message)
    settings = Settings(ntfy_topic="", mac_notifications=False)
    _sign_in(obtained_at=time.time() - 6.5 * 86400)
    gmail._write_secret(gmail.token_file(), {**gmail.token_info(), "expires_at": 0})
    feed = EmailAlerts("indeed", settings)
    with respx.mock:
        respx.post("https://oauth2.googleapis.com/token").respond(400, json={"error": "invalid_grant"})
        s = await FeedRunner(conn, settings, client, feeds=[feed]).run_once()
        conn.execute("UPDATE feeds SET next_poll=0")
        await FeedRunner(conn, settings, client, feeds=[feed]).run_once()
    assert s.errors == 1
    assert notices == ["ApplyPilot: Gmail sign-in ends tomorrow", "ApplyPilot: Gmail sign-in expired"]
