import time

import respx

from applypilot import db, notify
from applypilot.poller import Poller

GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def gh_job(i, title="Software Engineer, New Grad", loc="Pittsburgh, PA", age_s=60):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - age_s))
    return {"id": i, "title": title, "location": {"name": loc}, "absolute_url": f"https://acme.com/{i}",
            "company_name": "Acme", "first_published": ts, "updated_at": ts}


def force_due(conn):
    conn.execute("UPDATE boards SET next_poll=0")


async def test_baseline_then_new_job_is_pushed(conn, settings, client, monkeypatch):
    pushed = []

    async def fake_push(_c, _s, job, reason):
        pushed.append((job.title, reason))

    monkeypatch.setattr(notify, "push", fake_push)
    db.add_board(conn, "greenhouse", "acme", "Acme", "warm")
    p = Poller(conn, settings, client)

    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1), gh_job(2, "Senior Engineer")]}, headers={"etag": "e1"})
        s1 = await p.run_once()
    # first scan = baseline: recorded, nothing pushed, fresh relevant job surfaced in the digest
    assert s1.new_jobs == 2 and s1.relevant == [] and len(s1.fresh_baseline) == 1 and pushed == []

    force_due(conn)
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1), gh_job(2, "Senior Engineer"), gh_job(3, "ML Engineer")]})
        respx.get(f"{GH}/3").respond(200, json={"content": "&lt;p&gt;We sponsor visas&lt;/p&gt;"})
        s2 = await p.run_once()
    assert [j.title for j, _ in s2.relevant] == ["ML Engineer"]
    # the description is screened: "We sponsor visas" shows up as a visa signal on the push
    assert len(pushed) == 1 and pushed[0][0] == "ML Engineer"
    assert pushed[0][1].startswith("tech role, US · score ") and pushed[0][1].endswith("sponsors visas")
    row = conn.execute("SELECT description, notified_at, is_baseline FROM jobs WHERE ext_id='3'").fetchone()
    assert row["description"] == "We sponsor visas" and row["notified_at"] and row["is_baseline"] == 0
    assert s2.latencies_s and s2.latencies_s[0] < 600
    # a board that just posted a relevant role is promoted to the hot tier
    assert conn.execute("SELECT tier FROM boards").fetchone()[0] == "hot"


async def test_unchanged_board_costs_one_304(conn, settings, client):
    db.add_board(conn, "greenhouse", "acme")
    p = Poller(conn, settings, client, push=False)
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1)]}, headers={"etag": "e1"})
        await p.run_once()
    force_due(conn)
    with respx.mock:
        route = respx.get(GH).respond(304)
        s = await p.run_once()
    assert s.unchanged == 1 and route.calls[0].request.headers["if-none-match"] == "e1"
    nxt = conn.execute("SELECT next_poll FROM boards").fetchone()[0]
    assert nxt > time.time() + 0.8 * settings.tiers.warm


async def test_closed_and_reopened_jobs_are_not_new(conn, settings, client):
    db.add_board(conn, "greenhouse", "acme")
    p = Poller(conn, settings, client, push=False)
    for jobs in ([gh_job(1)], [], [gh_job(1)]):
        force_due(conn)
        with respx.mock:
            respx.get(GH).respond(200, json={"jobs": jobs})
            s = await p.run_once()
    assert s.new_jobs == 0
    assert conn.execute("SELECT closed_at FROM jobs").fetchone()[0] is None


async def test_board_dies_after_three_404s(conn, settings, client):
    db.add_board(conn, "greenhouse", "acme")
    p = Poller(conn, settings, client, push=False)
    for _ in range(3):
        force_due(conn)
        with respx.mock:
            respx.get(GH).respond(404)
            await p.run_once()
    assert conn.execute("SELECT tier FROM boards").fetchone()[0] == "dead"
    assert db.due_boards(conn, time.time() + 10**6) == []


async def test_board_without_us_jobs_is_demoted_to_cold(conn, settings, client):
    db.add_board(conn, "greenhouse", "acme", tier="hot")
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1, loc="London, United Kingdom")]})
        await Poller(conn, settings, client, push=False).run_once()
    assert conn.execute("SELECT tier FROM boards").fetchone()[0] == "cold"


def test_blocked_board_is_not_resurrected_by_reseeding(conn):
    db.add_board(conn, "lever", "neon", "Neon", "hot")
    db.set_tier(conn, "lever", "neon", "blocked")
    assert db.add_board(conn, "lever", "neon", "Neon", "hot") is False
    assert conn.execute("SELECT tier FROM boards").fetchone()[0] == "blocked"
    assert db.due_boards(conn, 1e12) == []


async def test_reseeding_does_not_undo_cold_demotion(conn, settings, client):
    db.add_board(conn, "greenhouse", "acme", tier="hot")
    with respx.mock:
        respx.get(GH).respond(200, json={"jobs": [gh_job(1, loc="London, United Kingdom")]})
        await Poller(conn, settings, client, push=False).run_once()
    db.add_board(conn, "greenhouse", "acme", tier="hot", source="yc")
    assert conn.execute("SELECT tier FROM boards").fetchone()[0] == "cold"
