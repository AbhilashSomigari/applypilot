import httpx
import respx
from conftest import load

from applypilot.models import ATS, Board
from applypilot.sources import SOURCES
from applypilot.sources.base import html_to_text

GH = "https://boards-api.greenhouse.io/v1/boards"


@respx.mock
async def test_greenhouse_list_and_details(client):
    respx.get(f"{GH}/stripe/jobs").respond(200, json=load("greenhouse_jobs.json"), headers={"etag": 'W/"a"'})
    r = await SOURCES[ATS.GREENHOUSE].poll(client, Board(ATS.GREENHOUSE, "stripe"))
    assert r.status == 200 and r.etag == 'W/"a"' and len(r.jobs) == 3
    j = r.jobs[0]
    assert j.company == "Stripe" and j.title == "Abuse Investigator"
    assert j.apply_url == f"https://job-boards.greenhouse.io/stripe/jobs/{j.ext_id}"
    assert j.posted_at and j.posted_at > 1.7e9
    assert j.description == ""  # list omits content

    respx.get(f"{GH}/stripe/jobs/{j.ext_id}").respond(200, json=load("greenhouse_job.json"))
    await SOURCES[ATS.GREENHOUSE].details(client, j)
    assert "Who we are" in j.description and "<" not in j.description


@respx.mock
async def test_conditional_get_returns_304(client):
    route = respx.get(f"{GH}/stripe/jobs").respond(304)
    r = await SOURCES[ATS.GREENHOUSE].poll(client, Board(ATS.GREENHOUSE, "stripe", etag='W/"a"'))
    assert r.status == 304 and r.etag == 'W/"a"' and r.jobs == []
    assert route.calls[0].request.headers["if-none-match"] == 'W/"a"'


@respx.mock
async def test_lever_parses_country_and_description(client):
    respx.get("https://api.lever.co/v0/postings/palantir?mode=json").respond(200, json=load("lever_postings.json"))
    r = await SOURCES[ATS.LEVER].poll(client, Board(ATS.LEVER, "palantir", "Palantir"))
    assert [j.country for j in r.jobs] == ["SG", "GB", "US"]
    assert r.jobs[2].apply_url.endswith("/apply")
    assert r.jobs[0].description and r.jobs[0].posted_at > 1.7e9


@respx.mock
async def test_ashby_marks_us_from_secondary_locations(client):
    respx.get("https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true").respond(
        200, json=load("ashby_board.json")
    )
    r = await SOURCES[ATS.ASHBY].poll(client, Board(ATS.ASHBY, "ramp", "Ramp"))
    assert len(r.jobs) == 3
    j = r.jobs[0]
    assert j.title == "Security Engineer, Cloud" and j.country == "US" and j.remote
    assert j.description.startswith("Compensation:")


@respx.mock
async def test_exists(client):
    respx.get(f"{GH}/stripe").respond(200, json={"name": "Stripe", "content": ""})
    respx.get(f"{GH}/nope").respond(404)
    respx.get("https://api.lever.co/v0/postings/nope?mode=json&limit=1").respond(404)
    assert await SOURCES[ATS.GREENHOUSE].exists(client, "stripe") == "Stripe"
    assert await SOURCES[ATS.GREENHOUSE].exists(client, "nope") is None
    assert await SOURCES[ATS.LEVER].exists(client, "nope") is None


def test_html_to_text_unescapes_greenhouse_double_escaping():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;&lt;li&gt;x&lt;/li&gt;") == "Hello & bye\n\nx"


async def test_network_error_is_not_raised_by_poller(conn, settings, client):
    from applypilot import db
    from applypilot.poller import Poller

    db.add_board(conn, "greenhouse", "stripe")
    with respx.mock:
        respx.get(f"{GH}/stripe/jobs").mock(side_effect=httpx.ConnectError("down"))
        stats = await Poller(conn, settings, client).run_once()
    assert stats.errors == 1
