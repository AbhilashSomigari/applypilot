"""Headless resume tailoring: Claude Code's `/resumetailor` skill run with `claude -p` in batch mode.

The skill (`~/.claude/skills/resumetailor/SKILL.md`) is interactive: it asks about unconfirmed tools and
records answers in the resume notes. The batch rules below are passed in the prompt, so the skill file
is never changed: no questions, no writes outside the output folder, a round budget, and a final
`BATCH_RESULT {...}` line this module parses.

Used for the 6 pre-built variants (`variants build`) and for one job at a time (`tailor JOB_ID`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from applypilot.config import Settings, home

VARIANTS = ("swe-backend", "ai-engineer", "ml-engineer", "infra-platform", "fullstack-fde", "data")

BATCH_RULES = """Batch mode (no person is present). Follow the resumetailor workflow with these overrides:
- Output folder: {out}. The JD is {out}/jd.txt. Copy the master there and work only inside {out}.
- Never call AskUserQuestion. Do not add bucket-B (plausible but unconfirmed) tools; list them under
  "Unconfirmed tools (ask later)" in report.md.
- Write nothing outside {out}: not the resume CLAUDE.md, not the scorer, not the master. Scorer vocabulary
  gaps go under "Scorer gaps" in report.md. Never report an inflated score.
- Put knockouts at the top of report.md and still tailor.
- Budget: stop at a score >= 90 or after 6 compile-and-score rounds, keeping the best 1-page version that
  passes every verification check.
- Repo sweep: use the project ranking and the local repo copies; call gh only for a repo that isn't local.
- Finish by writing {out}/final_score.txt (the number only) and printing this as the very last line:
  BATCH_RESULT {{"score": <int>, "pdf": "<path to the PDF>", "pages": <int>, "knockouts": [...],
  "unconfirmed": [...]}}
  On failure print: BATCH_RESULT {{"error": "<reason>"}}
- All hard rules still apply: never fabricate employers, titles, dates, metrics or tools."""

# what the headless session may do without asking: edit files and run the build, scorer and PDF checks
ALLOWED_TOOLS = ("Read Write Edit Glob Grep Bash(tectonic:*) Bash(python3:*) Bash(cp:*) Bash(mkdir:*) Bash(ls:*) "
                 "Bash(gh repo:*) Bash(gh api:*)")


@dataclass
class TailorResult:
    out: Path
    score: int | None = None
    pdf: Path | None = None
    knockouts: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    seconds: float = 0.0
    error: str | None = None


def resume_dir(settings: Settings) -> Path:
    return Path(settings.tailor.resume_dir).expanduser()


def resume_filename() -> str:
    """The master PDF's file name (recruiters see it), from profile.yaml `resume.master_pdf`."""
    path = home() / "profile.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    master = ((data or {}).get("resume") or {}).get("master_pdf") or "resume.pdf"
    return Path(master).name


def variant_dir(settings: Settings, name: str) -> Path:
    return resume_dir(settings) / "variants" / name


def variant_pdf(settings: Settings, name: str) -> Path:
    return variant_dir(settings, name) / resume_filename()


def slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower()).strip("-")[:80]


def command(settings: Settings, out: Path) -> list[str]:
    prompt = f"/resumetailor --batch --out {out}\n\n" + BATCH_RULES.format(out=out)
    cmd = [settings.tailor.claude_bin, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", ALLOWED_TOOLS]
    for d in settings.tailor.repo_dirs:
        if (p := Path(d).expanduser()).is_dir():
            cmd += ["--add-dir", str(p)]
    return cmd


def _env() -> dict[str, str]:
    """The skill's checks need the user's own python3 (PyMuPDF, pdfminer), not this project's venv."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    parts = [p for p in env.get("PATH", "").split(os.pathsep)
             if not (venv and p.startswith(venv)) and "/.venv/" not in p]
    env["PATH"] = os.pathsep.join(parts)
    env.pop("PYTHONPATH", None)
    return env


def parse(stdout: str, out: Path) -> TailorResult:
    res = TailorResult(out)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = {"result": stdout}
    res.cost_usd = envelope.get("total_cost_usd")
    text = envelope.get("result") or ""
    line = next((ln for ln in reversed(text.splitlines()) if ln.strip().startswith("BATCH_RESULT")), None)
    if envelope.get("is_error") or line is None:
        res.error = (text.strip().splitlines() or ["no output"])[-1][:300] if line is None else text[:300]
        return res
    try:
        data = json.loads(line.split("BATCH_RESULT", 1)[1])
    except json.JSONDecodeError:
        res.error = f"unparseable result line: {line[:200]}"
        return res
    if "error" in data:
        res.error = str(data["error"])
        return res
    res.score = int(data["score"]) if data.get("score") is not None else None
    res.pdf = Path(data["pdf"]) if data.get("pdf") else None
    res.knockouts = list(data.get("knockouts") or [])
    res.unconfirmed = list(data.get("unconfirmed") or [])
    if res.pdf is None or not res.pdf.exists():
        res.error = f"PDF not found: {res.pdf}"
    return res


async def run(settings: Settings, out: Path) -> TailorResult:
    """Tailor the resume to `out/jd.txt` (the folder must already hold it)."""
    if not (out / "jd.txt").exists():
        return TailorResult(out, error=f"missing {out / 'jd.txt'}")
    start = time.time()
    proc = await asyncio.create_subprocess_exec(
        *command(settings, out), cwd=resume_dir(settings), env=_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    # the deadline is wall-clock time: asyncio's monotonic clock stops while the Mac sleeps, so a plain
    # wait_for timeout let a run that slept mid-session hang for 5.3 h (2026-10-22)
    deadline = start + settings.tailor.timeout_s
    task = asyncio.ensure_future(proc.communicate())
    while not task.done():
        await asyncio.wait({task}, timeout=min(30.0, max(0.1, deadline - time.time())))
        if not task.done() and time.time() >= deadline:
            proc.kill()
            await proc.wait()
            task.cancel()
            return TailorResult(out, error=f"timed out after {settings.tailor.timeout_s} s",
                                seconds=time.time() - start)
    stdout, stderr = task.result()
    res = parse(stdout.decode(errors="replace"), out)
    if proc.returncode and not res.error:
        res.error = f"claude exited {proc.returncode}: {stderr.decode(errors='replace')[-300:]}"
    res.seconds = time.time() - start
    (out / "batch_result.json").write_text(json.dumps({
        "score": res.score, "pdf": str(res.pdf) if res.pdf else None, "knockouts": res.knockouts,
        "unconfirmed": res.unconfirmed, "cost_usd": res.cost_usd, "seconds": round(res.seconds),
        "error": res.error}, indent=2))
    return res


def write_jd(out: Path, title: str, company: str, url: str, description: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    # the scorer takes the job title from the first line
    (out / "jd.txt").write_text(f"{title}\n{company}\n{url}\n\n{description}\n")
    return out
