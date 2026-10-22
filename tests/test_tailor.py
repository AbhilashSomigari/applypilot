import json
import sys
import time

from applypilot import db, fit, rank, tailor
from applypilot.config import Settings, Tailor
from applypilot.models import ATS, Job

# stands in for `claude -p`: writes the PDF and score into --out, prints the JSON envelope
FAKE_CLAUDE = """
import json, os, re, sys
out = re.search(r"--out (\\S+)", sys.argv[2]).group(1)
open(f"{out}/Resume.pdf", "w").write("PDF")
open(f"{out}/final_score.txt", "w").write("91")
open(f"{out}/env.txt", "w").write("\\n".join(f"{k}={v}" for k, v in os.environ.items()))
line = {"score": 91, "pdf": f"{out}/Resume.pdf", "pages": 1, "knockouts": ["onsite NYC"], "unconfirmed": ["Spark"]}
print(json.dumps({"is_error": False, "total_cost_usd": 0.42, "result": "Done.\\nBATCH_RESULT " + json.dumps(line)}))
"""

# stands in for ats_score.py: the score is the number of JD words found in the "PDF" text
FAKE_SCORER = """
calls = []
def pdf_text(path):
    calls.append(path)
    return open(path).read(), 1
def score(resume_pdf, jd):
    text, _ = pdf_text(resume_pdf)
    words = set(text.lower().split())
    return {"total": 60 + 10 * sum(w in words for w in set(jd.lower().split()))}
"""


def _settings(tmp_path, **kw):
    return Settings(ntfy_topic="", mac_notifications=False, tailor=Tailor(resume_dir=str(tmp_path), **kw))


def test_parse_result_line_and_failures(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_text("x")
    ok = json.dumps({"result": f'…\nBATCH_RESULT {{"score": 93, "pdf": "{pdf}", "pages": 1, "knockouts": []}}',
                     "total_cost_usd": 1.5})
    r = tailor.parse(ok, tmp_path)
    assert (r.score, r.pdf, r.cost_usd, r.error) == (93, pdf, 1.5, None)
    assert tailor.parse(json.dumps({"result": 'BATCH_RESULT {"error": "no jd"}'}), tmp_path).error == "no jd"
    assert tailor.parse(json.dumps({"result": "I could not finish"}), tmp_path).error == "I could not finish"
    gone = json.dumps({"result": 'BATCH_RESULT {"score": 90, "pdf": "/nope.pdf"}'})
    assert tailor.parse(gone, tmp_path).error.startswith("PDF not found")


def test_command_passes_batch_rules_and_limits_tools(tmp_path):
    cmd = tailor.command(_settings(tmp_path), tmp_path / "out")
    prompt = cmd[2]
    # resolved to a full path, so it also works under launchd's minimal PATH
    assert cmd[0].endswith("claude") and cmd[1] == "-p"
    assert prompt.startswith(f"/resumetailor --batch --out {tmp_path / 'out'}")
    assert "Never call AskUserQuestion" in prompt and "BATCH_RESULT" in prompt
    tools = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash(tectonic:*)" in tools and "Bash(rm" not in tools and "Bash(git" not in tools
    (tmp_path / "opspilot").mkdir()
    cmd = tailor.command(_settings(tmp_path, repo_dirs=[str(tmp_path / "opspilot"), "/no/such/dir"]), tmp_path)
    assert cmd[-2:] == ["--add-dir", str(tmp_path / "opspilot")]


async def test_run_with_a_fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    monkeypatch.setenv("VIRTUAL_ENV", "/x/.venv")
    s = _settings(tmp_path, claude_bin=str(script))
    out = tailor.write_jd(tmp_path / "variants" / "ml-engineer", "ML Engineer", "Quora", "https://q", "PyTorch")
    r = await tailor.run(s, out)
    assert r.error is None and r.score == 91 and r.knockouts == ["onsite NYC"] and r.unconfirmed == ["Spark"]
    assert (out / "jd.txt").read_text().startswith("ML Engineer\nQuora\n")
    assert json.loads((out / "batch_result.json").read_text())["cost_usd"] == 0.42
    assert "VIRTUAL_ENV=" not in (out / "env.txt").read_text()  # the skill needs the user's own python3


async def test_run_without_a_jd_fails_fast(tmp_path):
    r = await tailor.run(_settings(tmp_path), tmp_path / "empty")
    assert r.error.startswith("missing")


def _variants(tmp_path, monkeypatch):
    scorer = tmp_path / "ats_score.py"
    scorer.write_text(FAKE_SCORER)
    monkeypatch.setattr(tailor, "resume_filename", lambda: "Resume.pdf")
    for name, text in {"ml-engineer": "pytorch models training", "swe-backend": "go services apis"}.items():
        d = tmp_path / "variants" / name
        d.mkdir(parents=True)
        (d / "Resume.pdf").write_text(text)
    fit._cache.clear()
    return _settings(tmp_path, scorer=str(scorer))


def test_fitter_picks_the_best_variant_and_caches_text(tmp_path, monkeypatch):
    s = _variants(tmp_path, monkeypatch)
    f = fit.get(s)
    assert f.best("Machine Learning Engineer\nTrain pytorch models") == ("ml-engineer", 80)
    f.best("Backend\nGo services")
    assert len(f.mod.calls) == 2  # each variant PDF read once
    fit._cache.clear()


def test_no_variants_means_no_fit(tmp_path):
    fit._cache.clear()
    assert fit.get(_settings(tmp_path, scorer=str(tmp_path / "missing.py"))) is None


def test_screen_adds_fit_to_score_and_label(tmp_path, monkeypatch, conn):
    s = _variants(tmp_path, monkeypatch)
    job = Job(ATS.ASHBY, "acme", "1", "ML Engineer", "Acme", "Remote", posted_at=time.time(),
              url="https://jobs.ashbyhq.com/acme/1", description="Train pytorch models")
    db.insert_jobs(conn, [job], time.time(), False)
    keep, signals = rank.screen(conn, job, settings=s)
    row = db.get_job(conn, job.id)
    assert keep and "fit 80 (ml-engineer)" in signals and (row["fit"], row["variant"]) == (80, "ml-engineer")
    assert row["score"] > rank.score(job, rank.assess(job.description), None)
    fit._cache.clear()


async def test_run_times_out_by_wall_clock(tmp_path):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(0o755)
    s = _settings(tmp_path, claude_bin=str(script), timeout_s=1)
    out = tailor.write_jd(tmp_path / "v", "SWE", "Acme", "", "Python")
    t = time.time()
    r = await tailor.run(s, out)
    assert r.error == "timed out after 1 s" and time.time() - t < 5
