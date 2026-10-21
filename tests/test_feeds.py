import json
import time

import respx
from conftest import load

from applypilot import db, notify
from applypilot.feeds import FeedRunner
from applypilot.feeds.github_lists import GitHubList
from applypilot.feeds.hn import HackerNews, parse_post
from applypilot.models import ATS, Job

RAW = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
U1 = "452afc2e-0c79-41f8-8201-1aab7df775db"


def row(i, url, title="Software Engineer, New Grad", company="Acme", sponsorship="Other", age_s=3600, **kw):
    return {"id": f"id-{i}", "company_name": company, "title": title, "url": url, "locations": ["New York, NY"],
            "active": True, "is_visible": True, "sponsorship": sponsorship, "date_posted": int(time.time() - age_s),
            **kw}


def no_probes():
    # every ATS existence probe misses (keeps tests offline and deterministic)
    respx.get(url__regex=r"https://(boards-api\.greenhouse\.io/v1/boards/[^/]+|api\.lever\.co/v0/postings/[^/?]+"
                         r"(\?mode=json&limit=1)?|api\.ashbyhq\.com/posting-api/job-board/[^/?]+)$").respond(404)


def runner(conn, settings, client, feed, push=True):
    return FeedRunner(conn, settings, client, feeds=[feed], push=push)


async def test_github_list_baseline_then_new_listing_is_pushed(conn, settings, client, monkeypatch):
    pushed = []

    async def fake_push(_c, _s, job, reason):
        pushed.append((job.id, reason))

    monkeypatch.setattr(notify, "push", fake_push)
    feed = GitHubList("SimplifyJobs/New-Grad-Positions")
    r = runner(conn, settings, client, feed)
    first = [row(1, "https://job-boards.greenhouse.io/acme/jobs/11"),
             row(2, "https://jobs.apple.com/en-us/details/200", company="Apple", age_s=5 * 86400),
             {**row(3, "https://jobs.lever.co/x/" + U1), "active": False}]
    with respx.mock:
        respx.get(RAW).respond(200, json=first, headers={"etag": '"e1"'})
        no_probes()
        s1 = await r.run_once()
    # baseline: both active rows stored, nothing pushed, the fresh relevant one goes to the digest
    assert s1.new_jobs == 2 and s1.relevant == [] and len(s1.fresh_baseline) == 1 and pushed == []
    assert db.job_exists(conn, "greenhouse:acme:11")
    assert conn.execute("SELECT tier, source FROM boards WHERE slug='acme'").fetchone()[:] == ("hot", feed.name)

    conn.execute("UPDATE feeds SET next_poll=0")
    second = [*first, row(4, f"https://jobs.ashbyhq.com/quora/{U1}?utm_source=Simplify", company="Quora"),
              row(5, "https://job-boards.greenhouse.io/acme/jobs/12", sponsorship="U.S. Citizenship is Required")]
    with respx.mock:
        route = respx.get(RAW).respond(200, json=second, headers={"etag": '"e2"'})
        no_probes()
        s2 = await r.run_once()
    assert route.calls[0].request.headers["if-none-match"] == '"e1"'
    assert s2.leads == 2 and [j.id for j, _ in s2.relevant] == [f"ashby:quora:{U1}"]
    assert pushed == [(f"ashby:quora:{U1}", "new-grad tech role, US · via Simplify")]
    dropped = conn.execute("SELECT relevant, reason FROM jobs WHERE ext_id='12'").fetchone()
    assert tuple(dropped) == (0, "requires US citizenship")
    q = conn.execute(f"SELECT apply_url, source, notified_at FROM jobs WHERE id='ashby:quora:{U1}'").fetchone()
    assert q["apply_url"] == f"https://jobs.ashbyhq.com/quora/{U1}/application" and q["notified_at"]


async def test_listing_already_found_by_the_poller_is_not_pushed_twice(conn, settings, client, monkeypatch):
    pushed = []
    monkeypatch.setattr(notify, "push", lambda *a: pushed.append(a))
    db.insert_jobs(conn, [Job(ATS.GREENHOUSE, "acme", "11", "Software Engineer", "Acme")], time.time(), False)
    conn.execute("INSERT INTO feeds (name, baseline_done) VALUES ('github:SimplifyJobs/New-Grad-Positions', 1)")
    with respx.mock:
        respx.get(RAW).respond(200, json=[row(1, "https://boards.greenhouse.io/acme/jobs/11")])
        s = await runner(conn, settings, client, GitHubList("SimplifyJobs/New-Grad-Positions")).run_once()
    assert s.known_jobs == 1 and s.new_jobs == 0 and pushed == []
    assert conn.execute("SELECT source FROM jobs").fetchone()[0] == "ats"


async def test_unchanged_list_costs_one_304(conn, settings, client):
    feed = GitHubList("SimplifyJobs/New-Grad-Positions")
    conn.execute("INSERT INTO feeds (name, etag, baseline_done) VALUES (?, '\"e1\"', 1)", (feed.name,))
    with respx.mock:
        respx.get(RAW).respond(304)
        s = await runner(conn, settings, client, feed).run_once()
    assert s.unchanged == 1 and s.errors == 0
    assert db.feed_state(conn, feed.name)["next_poll"] > time.time() + 0.8 * feed.interval_s


async def test_workday_listing_is_stored_and_its_company_probed(conn, settings, client):
    wd = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA/Software-Engineer_JR1"
    with respx.mock:
        respx.get(RAW).respond(200, json=[row(1, wd, company="NVIDIA")])
        gh = respx.get("https://boards-api.greenhouse.io/v1/boards/nvidia").respond(404)
        no_probes()
        s = await runner(conn, settings, client, GitHubList("SimplifyJobs/New-Grad-Positions"), push=False).run_once()
    assert db.job_exists(conn, "workday:nvidia.wd5/NVIDIAExternalCareerSite:JR1")
    assert s.probed == 1 and gh.called


def test_hn_company_name():
    from applypilot.feeds.hn import company_name

    assert company_name("Column (YC S20)") == "Column"
    assert company_name("telli hiring in Eng, GTM, Design [Berlin]") == "telli"
    assert company_name("Acme Robotics - Pittsburgh") == "Acme Robotics"
    assert company_name("Oscilar.com") == "Oscilar.com"


def test_hn_parse_post():
    hits = load("hn_comments.json")["hits"]
    lead = parse_post(hits[0])
    assert lead.company == "ICEYE" and len(lead.urls) == 3 and lead.urls[0].startswith("https://jobs.ashbyhq.com/")
    plain = parse_post(hits[1])
    assert plain.company == "Treat Health" and plain.website == "treat.health"


async def test_hn_registers_linked_boards_and_probes_the_rest(conn, settings, client):
    thread = 49922569
    data = load("hn_comments.json")
    with respx.mock:
        respx.get("https://hn.algolia.com/api/v1/search_by_date", params={"tags": "story,author_whoishiring"}).respond(
            200, json={"hits": [{"objectID": str(thread), "title": "Ask HN: Who is hiring? (October 2026)"}]})
        comments = respx.get("https://hn.algolia.com/api/v1/search_by_date", params={"tags": f"comment,story_{thread}"})
        comments.respond(200, json=data)
        no_probes()
        r = runner(conn, settings, client, HackerNews())
        s = await r.run_once()
        assert comments.calls[0].request.url.params["numericFilters"] == "created_at_i>0"
        conn.execute("UPDATE feeds SET next_poll=0")
        await r.run_once()
    # ICEYE links its Ashby board; Treat Health only its own site (probed); the reply is skipped
    assert s.leads == 2 and s.new_boards == 1 and s.probed == 1 and s.new_jobs == 0
    board = conn.execute("SELECT ats, slug, tier, source FROM boards").fetchone()
    assert tuple(board) == ("ashby", "iceye", "warm", "hn")
    newest = max(h["created_at_i"] for h in data["hits"])
    assert json.loads(db.feed_state(conn, "hn")["cursor"]) == {"thread": thread, "since": newest}
    assert comments.calls[1].request.url.params["numericFilters"] == f"created_at_i>{newest}"
