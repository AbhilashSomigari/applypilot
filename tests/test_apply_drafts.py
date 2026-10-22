import json
import sys

from applypilot.apply import drafts
from applypilot.apply.answers import Answer
from applypilot.apply.drafts import Draft, check
from applypilot.apply.forms import Field
from applypilot.config import Settings, Tailor

FACTS = """## Verified career facts
- **Contact:** Somewhere · (555) 555-0100 · jane@example.com
- Built OpsPilot, reaching **93% accuracy vs. a 30% LLM-only baseline** on a 30-incident benchmark ($0.03 each)
## Bullet bank
- Published evalforge-agents to PyPI with **104 tests**
## LinkedIn profile audit
- not a fact source
"""


def test_facts_text_keeps_verified_sections_without_contact_details(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(FACTS)
    t = drafts.facts_text(Settings(tailor=Tailor(resume_dir=str(tmp_path))))
    assert "93% accuracy" in t and "104 tests" in t
    assert "555-0100" not in t and "LinkedIn profile audit" not in t


def test_grounded_draft_passes():
    d = check(Draft("q", "Why us?", "I built OpsPilot to 93% accuracy vs. a 30% baseline, at $0.03 per incident.",
                    ["93% accuracy vs. a 30% LLM-only baseline"]), FACTS, "We want 2+ years")
    assert d.ok


def test_invented_facts_and_numbers_are_flagged():
    d = check(Draft("q", "Why us?", "I cut latency by 45% across 12 services at Google.",
                    ["Worked at Google on latency"]), FACTS, "posting text")
    assert not d.ok
    assert any("cited fact not in your notes" in i for i in d.issues)
    assert any("45%" in i for i in d.issues) and any("12" in i for i in d.issues)


def test_numbers_from_the_posting_are_allowed():
    assert check(Draft("q", "Why?", "Your 2027 cohort fits my December graduation.", []), FACTS,
                 "Join our 2027 new-grad cohort").ok


def test_only_written_questions_are_drafted_never_legal_notices():
    essay = Answer(Field("a", "Why Anthropic?", "textarea", True), None, "needs_you", "written answer (essay)")
    legal = Answer(Field("b", "Agreement to Arbitrate", "select", True, ["I agree"]), None, "needs_you",
                   "legal agreement / attestation: read and decide")
    short = Answer(Field("c", "Why do you want to join Figma?", "text", True), None, "needs_you",
                   "no rule for this required question")
    years = Answer(Field("d", "Years of Python?", "select", True, ["0-1"]), None, "needs_you", "no rule")
    assert [drafts.draftable(a) for a in (essay, legal, short, years)] == [True, False, True, False]


FAKE = """
import json, sys
schema = sys.argv[sys.argv.index("--json-schema") + 1]
assert sys.argv[sys.argv.index("--tools") + 1] == ""          # no tools
assert "Use ONLY facts" in sys.argv[sys.argv.index("--system-prompt") + 1]
print(json.dumps({"is_error": False, "structured_output": {"answers": [
    {"key": "a", "draft": "OpsPilot reached 93% accuracy.", "facts_used": ["93% accuracy"]},
    {"key": "c", "draft": "I scaled a team of 40 engineers.", "facts_used": ["led 40 engineers"]}]}}))
"""


async def test_draft_end_to_end_with_a_fake_claude(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(FACTS)
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE}")
    script.chmod(0o755)
    s = Settings(tailor=Tailor(resume_dir=str(tmp_path), claude_bin=str(script)))
    plan = [Answer(Field("a", "Why Acme?", "textarea", True), None, "needs_you", "written answer (essay)"),
            Answer(Field("c", "Describe your leadership.", "textarea", True), None, "needs_you",
                   "written answer (essay)")]
    found = await drafts.draft(s, plan, "Acme", "SWE", "posting")
    assert [(d.key, d.ok) for d in found] == [("a", True), ("c", False)]
    out = drafts.apply_drafts(plan, found)
    assert out[0].source == "draft" and out[0].value == "OpsPilot reached 93% accuracy."
    assert "CHECK" in out[1].note  # flagged drafts are still shown to the user, never hidden


def test_parse_falls_back_to_json_in_the_result_text():
    env = {"result": 'Here you go: {"answers": [{"key": "a", "draft": "x", "facts_used": []}]}'}
    assert drafts.parse(json.dumps(env))[0]["key"] == "a"
