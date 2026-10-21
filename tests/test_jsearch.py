import time

import respx

from applypilot import db, notify, resolver
from applypilot.config import JSearch as JSearchSettings
from applypilot.feeds import FeedRunner
from applypilot.feeds.jsearch import JSearch, to_lead
from applypilot.models import ATS, Job

API = "https://jsearch.p.rapidapi.com/search"


def result(job_id, title, company, options, *, remote=False, age_s=1800, **kw):
    # field names as documented for JSearch /search (no live key yet; see docs/03 §3.3)
    return {"job_id": job_id, "job_title": title, "employer_name": company, "employer_website": None,
            "job_publisher": options[0]["publisher"] if options else "", "job_apply_link": options[0]["apply_link"],
            "job_apply_is_direct": options[0]["is_direct"], "apply_options": options, "job_is_remote": remote,
            "job_city": "New York", "job_state": "NY", "job_country": "US",
            "job_posted_at_timestamp": int(time.time() - age_s), "job_description": "Build things.", **kw}


def opt(publisher, link, direct=False):
    return {"publisher": publisher, "apply_link": link, "is_direct": direct}


def cfg(**kw):
    return JSearchSettings(api_key="k", queries=["software engineer new grad"], **kw)


def test_interval_is_paced_to_the_monthly_quota():
    free = JSearchSettings(api_key="k")  # 4 queries, 200 requests
    assert free.interval_s() == int(30 * 86400 * 4 / 180)  # ~16 h per query
    pro = JSearchSettings(api_key="k", monthly_requests=10_000)
    assert pro.interval_s() == 1152
    assert JSearchSettings(api_key="k", monthly_requests=10**6).interval_s() == 900  # min_interval floor


def test_slow_pacing_widens_the_date_window():
    assert JSearch("q", JSearchSettings(api_key="k")).interval_s > 6 * 3600  # free plan: ~16 h
    assert resolver.is_aggregator("https://jobgether.com/offer/abc") and resolver.is_aggregator("https://remote.co/x")


def test_to_lead_orders_direct_links_first_and_marks_remote():
    lead = to_lead(result("a", "Software Engineer I", "Acme", [
        opt("LinkedIn", "https://www.linkedin.com/jobs/view/1"),
        opt("Acme Careers", "https://job-boards.greenhouse.io/acme/jobs/9", True)], remote=True))
    assert lead.urls[0] == "https://job-boards.greenhouse.io/acme/jobs/9"
    assert lead.location == "New York, NY (Remote)" and lead.country == "US" and lead.description
    assert "job_description" not in lead.raw


def test_best_url_prefers_pollable_ats_then_any_ats_then_employer_page():
    li, ind = "https://www.linkedin.com/jobs/view/1", "https://www.indeed.com/viewjob?jk=2"
    wd = "https://acme.wd1.myworkdayjobs.com/External/job/NY/SWE_R1"
    gh = "https://job-boards.greenhouse.io/acme/jobs/9"
    assert resolver.best_url([li, ind]) is None
    assert resolver.best_url([li, "https://acme.com/careers/1", wd, gh]) == gh
    assert resolver.best_url([li, "https://acme.com/careers/1", wd]) == wd
    assert resolver.best_url([ind, "https://acme.com/careers/1"]) == "https://acme.com/careers/1"


async def test_jsearch_feed_end_to_end(conn, settings, client, monkeypatch):
    pushed = []

    async def fake_push(_c, _s, job, reason):
        pushed.append((job.id, reason))

    monkeypatch.setattr(notify, "push", fake_push)
    # Acme is tracked and its board baseline already holds the job LinkedIn will point to
    db.add_board(conn, "greenhouse", "acme", "Acme Inc", "warm")
    conn.execute("UPDATE boards SET baseline_done=1")
    db.insert_jobs(conn, [Job(ATS.GREENHOUSE, "acme", "7", "Backend Engineer, New Grad", "Acme", "New York, NY",
                              posted_at=time.time() - 3600,
                              apply_url="https://job-boards.greenhouse.io/acme/jobs/7")], time.time(), True)
    db.set_relevance(conn, "greenhouse:acme:7", True, "new-grad tech role, US")
    feed = JSearch("software engineer new grad", cfg())
    conn.execute("INSERT INTO feeds (name, baseline_done) VALUES (?, 1)", (feed.name,))
    r = FeedRunner(conn, settings, client, feeds=[feed])
    page = {"status": "OK", "data": [
        # direct option on a new Ashby board -> new job, stored with the JSearch description
        result("j1", "Software Engineer, New Grad", "Quora", [
            opt("Indeed", "https://www.indeed.com/viewjob?jk=1"),
            opt("Quora", "https://jobs.ashbyhq.com/quora/452afc2e-0c79-41f8-8201-1aab7df775db", True)]),
        # aggregator-only -> matched on Acme's tracked board by company + title, pushed once
        result("j2", "Backend Engineer - New Grad", "ACME", [opt("LinkedIn", "https://www.linkedin.com/jobs/view/5")]),
    ]}
    with respx.mock:
        route = respx.get(API).respond(200, json=page)
        respx.get(url__regex=r"https://(boards-api\.greenhouse\.io|api\.lever\.co|api\.ashbyhq\.com)/.*").respond(404)
        s = await r.run_once()
    req = route.calls[0].request
    assert req.headers["x-rapidapi-key"] == "k" and req.url.params["date_posted"] == "today"
    assert s.resolved_by_name == 1 and s.new_jobs == 1 and s.known_jobs == 1
    quora = "ashby:quora:452afc2e-0c79-41f8-8201-1aab7df775db"
    assert sorted(j for j, _ in pushed) == [quora, "greenhouse:acme:7"]
    assert dict(pushed)[quora].endswith("· via Google Jobs")
    assert db.get_job(conn, quora)["description"] == "Build things."
    assert db.get_job(conn, "greenhouse:acme:7")["notified_at"]

    # the same job surfacing again (new JSearch id) is not pushed twice
    conn.execute("UPDATE feeds SET next_poll=0")
    page["data"] = [{**page["data"][1], "job_id": "j3"}]
    with respx.mock:
        respx.get(API).respond(200, json=page)
        await r.run_once()
    assert len(pushed) == 2


async def test_free_plan_asks_for_three_days(client):
    with respx.mock:
        route = respx.get(API).respond(200, json={"status": "OK", "data": []})
        await JSearch("q", JSearchSettings(api_key="k")).fetch(client, None, None)
    assert route.calls[0].request.url.params["date_posted"] == "3days"


async def test_quota_exhausted_backs_off(conn, settings, client):
    feed = JSearch("q", cfg())
    with respx.mock:
        respx.get(API).respond(429, json={"message": "You have exceeded the MONTHLY quota"})
        s = await FeedRunner(conn, settings, client, feeds=[feed]).run_once()
    assert s.errors == 1
    st = db.feed_state(conn, feed.name)
    assert st["fail_count"] == 1 and st["next_poll"] > time.time() + 1.5 * feed.interval_s


async def test_openwebninja_direct_uses_x_api_key_and_falls_back_to_v2(client):
    feed = JSearch("q", cfg(provider="openwebninja"))
    base = "https://api.openwebninja.com/jsearch/search"
    with respx.mock:
        v1 = respx.get(base).respond(404)
        v2 = respx.get(f"{base}-v2").respond(200, json={"status": "OK", "data": [
            result("j1", "Software Engineer I", "Acme", [opt("Acme", "https://acme.com/jobs/1", True)])]})
        r = await feed.fetch(client, None, None)
    assert v1.calls[0].request.headers["x-api-key"] == "k" and v2.called
    assert r.status == 200 and r.leads[0].company == "Acme"
