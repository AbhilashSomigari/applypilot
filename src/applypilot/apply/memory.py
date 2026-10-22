"""Answer memory: what the user typed or picked for a question once, reused for the same question later.

Stored in `~/.applypilot/answer_memory.yaml` (plain YAML, edit or delete freely). Rules:
- Legal notices and attestations are never stored: the user chose to be asked every time.
- Essays (multi-line written answers) aren't stored: they are written for one company.
- Questions about the user's history *with one company* ("interviewed here before", "current X employee")
  are stored for that company only.
- A stored answer is reused only for the same question (company names ignored, near-identical wording),
  and for a dropdown only when the new form offers that exact option. Rules backed by answers.yaml win.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass

import yaml

from applypilot.apply.forms import Field
from applypilot.config import home

MATCH = 0.92  # wording similarity needed to reuse an answer (after normalization)
# word boundaries matter: "referred" must not match inside "preferred"
_COMPANY_SCOPED = re.compile(r"\b(interviewed|applied|previously|before|current(ly)? (an? )?\w* ?(employee|contractor"
                             r"|intern)|employed by|work(ed)? (for|at)|referr(ed|al)|relatives?|family members?)\b",
                             re.I)
_NEVER = re.compile(r"arbitrat|agree|acknowledg|consent|certif|attest|i understand|privacy (notice|policy)|ai policy"
                    r"|notetak|record(ing|s)? (of )?(your |the )?interview|information policy|terms", re.I)


@dataclass
class Memory:
    question: str  # normalized
    example: str  # the wording it was learned from
    answer: str
    kind: str
    company: str | None = None  # set when it only holds for one company
    learned: str = ""


def path():
    return home() / "answer_memory.yaml"


def normalize(label: str, company: str = "") -> str:
    s = label.lower()
    for name in {company.lower(), re.sub(r",? (inc|llc|corp|co)\.?$", "", company.lower())} - {""}:
        s = s.replace(name, "<company>")
    s = re.sub(r"[*✱]|\(required\)|\(optional\)", " ", s)
    s = re.sub(r"[^a-z0-9<>+#/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def storable(f: Field, value: str) -> bool:
    if not value or f.kind == "file" or _NEVER.search(f.label):
        return False
    return not (f.kind == "textarea" and len(value) > 120)  # an essay, not a fact


def load() -> list[Memory]:
    p = path()
    if not p.exists():
        return []
    return [Memory(**m) for m in (yaml.safe_load(p.read_text()) or [])]


def save(items: list[Memory]) -> None:
    head = ("# Answers you gave on application forms, reused when the same question comes up again.\n"
            "# Edit or delete entries freely. Legal notices and essays are never stored.\n"
            "# `company:` set = this answer is only reused for that company.\n")
    path().write_text(head + yaml.safe_dump([m.__dict__ for m in items], sort_keys=False, allow_unicode=True))


def remember(f: Field, value: str, company: str, items: list[Memory] | None = None) -> Memory | None:
    """Store (or update) the answer to one question. Returns the entry, or None when it may not be stored."""
    if not storable(f, value):
        return None
    items = load() if items is None else items
    scoped = company if _COMPANY_SCOPED.search(f.label) else None
    q = normalize(f.label, company)
    entry = Memory(q, f.label.strip()[:200], value, f.kind, scoped, f"{time.strftime('%Y-%m-%d')} at {company}")
    items[:] = [m for m in items if not (m.question == q and m.company == scoped)] + [entry]
    save(items)
    return entry


def recall(f: Field, company: str, items: list[Memory] | None = None) -> Memory | None:
    """The stored answer for this question at this company, if one fits the field."""
    if _NEVER.search(f.label):
        return None
    items = load() if items is None else items
    q = normalize(f.label, company)
    best, score = None, 0.0
    for m in items:
        if m.company and m.company.lower() != company.lower():
            continue
        r = 1.0 if m.question == q else difflib.SequenceMatcher(None, m.question, q).ratio()
        if r > score:
            best, score = m, r
    if best is None or score < MATCH:
        return None
    if f.kind in ("select", "multiselect"):
        wanted = [v.strip() for v in best.answer.split(",")] if f.kind == "multiselect" else [best.answer]
        if not all(any(o.strip().lower() == w.lower() for o in f.options) for w in wanted):
            return None  # the option the user picked before isn't on this form
    return best
