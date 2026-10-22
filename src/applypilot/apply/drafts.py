"""Draft written answers ("Why X?", "Describe a project…") grounded only in the user's verified facts.

Facts come from the resume fact base (`<resume_dir>/CLAUDE.md`: verified career facts, project ranking,
bullet bank, keyword mapping, policy), the job posting is context. The draft is written by headless
Claude Code with **no tools** and a JSON schema, then checked here:
- every fact the model says it used must appear (normalized) in the fact text;
- every number in the draft must appear in the facts or the posting.
A draft that fails a check is still shown, flagged, so the user fixes it. Drafts are filled into the
form but stay outlined for review; the user still types `submit`. Legal notices are never drafted, and
drafts are never stored in answer memory.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from applypilot.apply.answers import Answer
from applypilot.config import Settings, home

DRAFTABLE = ("written answer (essay)", "written answer (two-part question)")
_WHY = re.compile(r"^\s*(why|describe|tell us|what (excites|interests|draws)|how would you|share an example"
                  r"|what makes you|in \d|please (describe|share|explain))", re.I)
_SECTIONS = ("Verified career facts", "Project ranking", "Bullet bank", "Keyword mapping", "Policy")

SYSTEM = """You draft answers to job-application questions for one candidate.
Rules, all mandatory:
- Use ONLY facts from the FACTS block. Never invent employers, titles, dates, metrics, tools, projects or
  outcomes. If the facts don't support an answer, write a shorter answer from what they do support.
- Use the JOB POSTING only to connect his real experience to the role and company. Don't claim knowledge
  of the company beyond the posting.
- First person, plain and specific, no buzzword padding, no greeting or sign-off, no em dashes.
- Respect any length the question asks for ("3-4 sentences", "under 500 characters"); otherwise 60-120
  words.
- For each answer, list in facts_used the exact phrases you relied on, copied verbatim from FACTS.
Return only the JSON the schema describes."""

SCHEMA = {"type": "object", "required": ["answers"], "properties": {"answers": {"type": "array", "items": {
    "type": "object", "required": ["key", "draft", "facts_used"], "properties": {
        "key": {"type": "string"}, "draft": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}}}}}}}


@dataclass
class Draft:
    key: str
    label: str
    text: str
    facts_used: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # empty = passed the grounding checks

    @property
    def ok(self) -> bool:
        return not self.issues


def draftable(a: Answer) -> bool:
    if a.source != "needs_you":
        return False
    if a.note in DRAFTABLE:
        return True
    return a.field.kind in ("text", "textarea") and a.note.startswith("no rule") and bool(_WHY.search(a.field.label))


def facts_text(settings: Settings) -> str:
    """The verified sections of the resume fact base (not file lists, LinkedIn audits or contact details)."""
    path = Path(settings.tailor.resume_dir).expanduser() / "CLAUDE.md"
    if not path.exists():
        return ""
    text = path.read_text()
    parts = re.split(r"(?m)^## ", text)
    keep = [p for p in parts if p.startswith(_SECTIONS)]
    body = "\n## ".join(keep)
    return re.sub(r"(?m)^- \*\*Contact:\*\*.*$", "", body)  # contact details aren't facts to write about


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%.$+]+", " ", s.lower()).strip()


def check(d: Draft, facts: str, posting: str) -> Draft:
    nf, np_ = _norm(facts), _norm(posting)
    for f in d.facts_used:
        if _norm(f) and _norm(f) not in nf:
            d.issues.append(f"cited fact not in your notes: {f[:80]!r}")
    for num in set(re.findall(r"\$?\d[\d,.]*%?x?", d.text)):
        n = num.rstrip(".,")
        bare = n.strip("$%x").replace(",", "")
        if not bare or len(bare) == 1 and bare in "0123456789" and not n.endswith(("%", "x")):
            continue  # single digits ("3 projects") are fine when the cited facts back them
        if _norm(n) not in nf and _norm(n) not in np_ and bare not in nf.replace(",", ""):
            d.issues.append(f"number not in your notes or the posting: {n}")
    if not d.text.strip():
        d.issues.append("empty draft")
    return d


def command(settings: Settings, prompt: str) -> list[str]:
    return [settings.tailor.claude_bin, "-p", prompt, "--output-format", "json", "--tools", "",
            "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", "1"]


def prompt_for(answers: list[Answer], company: str, title: str, posting: str, facts: str) -> str:
    qs = "\n".join(f"- key: {a.field.key}\n  question: {a.field.label}" for a in answers)
    return (f"FACTS (the only source of truth about the candidate):\n<<<\n{facts}\n>>>\n\n"
            f"JOB POSTING: {title} at {company}\n<<<\n{posting[:8000]}\n>>>\n\n"
            f"QUESTIONS to answer (one answer per key):\n{qs}\n")


def parse(stdout: str) -> list[dict]:
    env = json.loads(stdout)
    out = env.get("structured_output")
    if out is None:
        res = env.get("result") or ""
        m = re.search(r"\{.*\}", res, re.S)
        out = json.loads(m.group(0)) if m else {}
    return out.get("answers") or []


async def draft(settings: Settings, answers: list[Answer], company: str, title: str, posting: str) -> list[Draft]:
    """Draft every draftable answer in one headless call; returns checked drafts (possibly flagged)."""
    todo = [a for a in answers if draftable(a)]
    facts = facts_text(settings)
    if not todo or not facts:
        return []
    proc = await asyncio.create_subprocess_exec(
        *command(settings, prompt_for(todo, company, title, posting, facts)), cwd=str(home()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    deadline = time.time() + 240
    task = asyncio.ensure_future(proc.communicate())
    while not task.done():  # wall-clock deadline (B-022: a sleeping Mac stops asyncio's clock)
        await asyncio.wait({task}, timeout=min(15.0, max(0.1, deadline - time.time())))
        if not task.done() and time.time() >= deadline:
            proc.kill()
            await proc.wait()
            task.cancel()
            return []
    stdout, _ = task.result()
    try:
        got = {d["key"]: d for d in parse(stdout.decode(errors="replace"))}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []
    drafts = []
    for a in todo:
        d = got.get(a.field.key)
        if d:
            drafts.append(check(Draft(a.field.key, a.field.label, d.get("draft", "").strip(),
                                      list(d.get("facts_used") or [])), facts, posting))
    return drafts


def apply_drafts(plan: list[Answer], drafts: list[Draft]) -> list[Answer]:
    """Put drafts into the plan as source 'draft' (filled, still listed for the user to review)."""
    by_key = {d.key: d for d in drafts}
    out = []
    for a in plan:
        d = by_key.get(a.field.key)
        if d and d.text:
            note = "draft: read and edit" + ("" if d.ok else " · CHECK: " + "; ".join(d.issues))
            a = Answer(a.field, d.text, "draft", note)
        out.append(a)
    return out
