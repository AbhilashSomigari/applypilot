import time

import pytest
import respx

from applypilot import db, resolver
from applypilot.poller import Poller
from applypilot.resolver import Link, parse

U1 = "cf6ba0e9-a750-4ee7-9b0a-8cb519cb7c39"


@pytest.mark.parametrize(
    "url, ats, slug, ext_id",
    [
        ("https://job-boards.greenhouse.io/Figma/jobs/5976930004?gh_jid=5976930004", "greenhouse", "figma",
         "5976930004"),
        ("https://boards.greenhouse.io/spacex/jobs/8436708002?utm_source=Simplify", "greenhouse", "spacex",
         "8436708002"),
        ("https://boards.greenhouse.io/embed/job_app?for=aqr&token=8284812", "greenhouse", "aqr", "8284812"),
        ("https://job-boards.greenhouse.io/hevenaerotech?gh_src=rlbk86k09us", "greenhouse", "hevenaerotech", None),
        ("https://careers.aqr.com/jobs?gh_jid=8284812&gh_jid=8284812", "greenhouse", "", "8284812"),
        (f"https://jobs.lever.co/Palantir/{U1}/apply", "lever", "Palantir", U1),
        ("https://jobs.lever.co/palantir?team=Dev", "lever", "palantir", None),
        (f"https://jobs.ashbyhq.com/ICEYE/{U1.upper()}/application", "ashby", "iceye", U1),
        ("https://jobs.ashbyhq.com/iceye/embed", "ashby", "iceye", None),
        ("https://www.example.com/careers?ashby_jid=abc", "ashby", "", "abc"),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
         "Software-Engineer_JR1995432", "workday", "nvidia.wd5/NVIDIAExternalCareerSite", "JR1995432"),
        ("https://jobs.smartrecruiters.com/Intuitive/744000156658124-full-stack-engineer", "smartrecruiters",
         "intuitive", "744000156658124"),
        ("https://apply.workable.com/huggingface/j/ABC123/", "workable", "huggingface", "ABC123"),
    ],
)
def test_parse_known_ats_urls(url, ats, slug, ext_id):
    link = parse(url)
    assert (link.ats, link.slug, link.ext_id) == (ats, slug, ext_id)


def test_parse_other_urls_get_a_stable_id_without_tracking_params():
    a = parse("https://jobs.apple.com/en-us/details/200 ?utm_source=Simplify&ref=Simplify".replace(" ", ""))
    b = parse("https://jobs.apple.com/en-us/details/200")
    assert a.ats == "other" and a.slug == "jobs.apple.com" and a.ext_id == b.ext_id and a.url == b.url
    assert parse("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101").ats == "other"  # EU: not polled
    assert parse("mailto:jobs@acme.com") is None


def test_apply_urls():
    assert parse(f"https://jobs.lever.co/acme/{U1}").apply_url == f"https://jobs.lever.co/acme/{U1}/apply"
    assert parse("https://boards.greenhouse.io/embed/job_app?for=aqr&token=1").apply_url == \
        "https://job-boards.greenhouse.io/aqr/jobs/1"
    assert Link("other", "x.com", "h", "https://x.com/j").apply_url == "https://x.com/j"


@respx.mock
async def test_resolve_recovers_greenhouse_slug_and_adds_board(conn, client):
    respx.get("https://boards.greenhouse.io/embed/job_app?token=8284812").respond(
        301, headers={"location": "https://job-boards.greenhouse.io/embed/job_app?for=aqr&token=8284812"})
    link = await resolver.resolve(conn, client, "https://careers.aqr.com/jobs?gh_jid=8284812", "AQR", "github:x")
    assert link.job_id == "greenhouse:aqr:8284812" and link.new_board
    row = conn.execute("SELECT tier, source, company FROM boards").fetchone()
    assert tuple(row) == ("hot", "github:x", "AQR")


@respx.mock
async def test_resolve_follows_short_links(conn, client):
    respx.get("https://grnh.se/abc").respond(
        301, headers={"location": "https://job-boards.greenhouse.io/freeform/jobs/7688735003?gh_src=27a"})
    link = await resolver.resolve(conn, client, "https://grnh.se/abc", "Freeform", "hn", tier="warm")
    assert link.job_id == "greenhouse:freeform:7688735003"
    assert conn.execute("SELECT tier FROM boards WHERE slug='freeform'").fetchone()[0] == "warm"


@respx.mock
async def test_unrecoverable_slug_degrades_to_other(conn, client):
    respx.get("https://boards.greenhouse.io/embed/job_app?token=1").respond(404)
    link = await resolver.resolve(conn, client, "https://acme.com/jobs?gh_jid=1")
    assert link.ats == "other" and link.slug == "acme.com" and link.job_id
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0


async def test_embedded_ashby_job_uses_the_companys_known_board(conn, client):
    db.add_board(conn, "ashby", "acme", "Acme Inc", "warm")
    link = await resolver.resolve(conn, client, f"https://acme.com/careers?ashby_jid={U1}", "Acme")
    assert link.job_id == f"ashby:acme:{U1}"


def _add_job(conn, ext_id, title, loc="New York, NY", posted=None, slug="acme"):
    from applypilot.models import ATS, Job

    db.insert_jobs(conn, [Job(ATS.GREENHOUSE, slug, ext_id, title, "Acme", loc, posted_at=posted or time.time(),
                              apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{ext_id}")], time.time(), True)


def test_find_on_boards_matches_title_then_location(conn):
    db.add_board(conn, "greenhouse", "acme", "Acme, Inc.")
    _add_job(conn, "1", "Software Engineer, New Grad", "San Francisco, CA")
    _add_job(conn, "2", "Software Engineer, New Grad", "New York, NY")
    _add_job(conn, "3", "Senior Software Engineer")
    hit = resolver.find_on_boards(conn, "ACME", "Software Engineer - New Grad", "New York, NY, United States")
    assert hit["ext_id"] == "2"
    assert resolver.find_on_boards(conn, "Acme", "Product Designer") is None
    assert resolver.find_on_boards(conn, "Other Co", "Software Engineer, New Grad") is None


def test_title_score():
    assert resolver.title_score("Software Engineer, New Grad", "software engineer new grad") == 1.0
    assert resolver.title_score("ML Engineer", "Account Executive") < 0.5


async def test_resolve_by_name_probes_and_scans_an_untracked_company(conn, settings, client):
    gh = "https://boards-api.greenhouse.io/v1/boards"
    with respx.mock:
        respx.get(f"{gh}/acme").respond(200, json={"name": "Acme"})
        respx.get(url__regex=r"https://api\.(lever\.co|ashbyhq\.com)/.*").respond(404)
        respx.get(f"{gh}/acme/jobs").respond(200, json={"jobs": [
            {"id": 7, "title": "Backend Engineer I", "location": {"name": "Remote, US"}, "company_name": "Acme"}]})
        p = Poller(conn, settings, client, push=False)
        hit = await resolver.resolve_by_name(conn, client, p, "Acme", "Backend Engineer I", source="gmail:linkedin")
    assert hit["id"] == "greenhouse:acme:7" and hit["apply_url"].endswith("/acme/jobs/7")
    assert conn.execute("SELECT baseline_done, tier FROM boards").fetchone()[:] == (1, "hot")
