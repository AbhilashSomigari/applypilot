import respx

from applypilot import db, seeds


def test_slug_candidates():
    assert seeds.slug_candidates("Ramp", "https://ramp.com") == ["ramp"]
    assert seeds.slug_candidates("Scale AI, Inc.", "scale.com") == ["scaleai", "scale-ai", "scale"]
    assert seeds.slug_candidates("WEIGHTS & BIASES INC") == ["weightsbiases", "weights-biases"]


def test_domain_root_skips_subdomains():
    assert seeds.domain_root("https://jobs.cable.tech/careers") == "cable"
    assert seeds.domain_root("www.acme.co.uk") == "acme"
    assert seeds.domain_root("figure.ai") == "figure"
    assert seeds.slug_candidates("Paradromics", "archive.paradromics.com") == ["paradromics"]


def test_company_key_ignores_legal_suffixes():
    assert seeds.company_key("GOOGLE LLC") == seeds.company_key("Google") == "google"
    assert seeds.company_key("Tata Consultancy Svcs Ltd") == "tataconsultancy"


def test_h1b_seeds(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(
        '"Fiscal Year",Employer,"Initial Approval","Initial Denial","Continuing Approval","Continuing Denial",'
        'NAICS,"Tax ID",State,City,ZIP\n'
        "2023,ACME INC,20,0,90,0,54,1,PA,PITTSBURGH,15201\n"
        "2023,ACME INC,5,0,0,0,54,1,NY,NEW YORK,10001\n"
        "2023,TINY LLC,1,0,0,0,54,2,WV,MORGANTOWN,26505\n"
    )
    out = seeds.h1b_seeds(p, min_approvals=25, hot_threshold=100)
    assert [(s.name, s.h1b_approvals, s.tier) for s in out] == [("Acme Inc", 115, "hot")]


@respx.mock
async def test_add_companies_probes_once_and_caches(conn, client):
    respx.get("https://boards-api.greenhouse.io/v1/boards/ramp").respond(404)
    respx.get("https://api.lever.co/v0/postings/ramp?mode=json&limit=1").respond(404)
    ashby = respx.get("https://api.ashbyhq.com/posting-api/job-board/ramp").respond(200, json={"jobs": []})
    c1 = await seeds.add_companies(conn, client, [seeds.Seed("Ramp", "ramp.com", "hot")])
    c2 = await seeds.add_companies(conn, client, [seeds.Seed("Ramp", "ramp.com", "hot")])
    assert c1["new_boards"] == 1 and c1["ashby"] == 1 and c2["probed"] == 0 and ashby.call_count == 1
    assert [(b.ats, b.slug, b.tier) for b in db.due_boards(conn, 1e12)] == [("ashby", "ramp", "hot")]


def test_names_match():
    assert not seeds.names_match("Oath Animal Hospital", "Oath Holdings Inc")
    assert seeds.names_match("BILL", "Bill Com")
    assert seeds.names_match("Social Finance", "Social Finance Inc")
    assert seeds.names_match("Stripe", "Stripe")
    assert seeds.names_match("Rubrik Job Board", "Rubrik Inc")
    assert seeds.names_match("Careers at Acme", "Acme")


def test_negative_probe_results_expire(conn):
    db.save_probe(conn, "acme", [], now=1000.0)
    db.save_probe(conn, "ramp", [["ashby", "ramp"]], now=1000.0)
    assert db.get_probe(conn, "acme", negative_ttl_s=100, now=1050.0) == []
    assert db.get_probe(conn, "acme", negative_ttl_s=100, now=2000.0) is None
    assert db.get_probe(conn, "ramp", negative_ttl_s=100, now=2000.0) == [["ashby", "ramp"]]
