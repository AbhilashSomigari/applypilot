import json

import pytest
from conftest import FIX

from applypilot.apply import answers, forms, memory
from applypilot.apply.forms import Field


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))


def F(label, kind="select", options=("0-1", "2-3", "4+")):
    return Field("q", label, kind, True, list(options))


def test_remember_and_recall_the_same_question_across_companies():
    memory.remember(F("How many years of professional experience do you have with Python?"), "0-1", "Affirm")
    m = memory.recall(F("How many years of professional experience do you have with Python? *"), "Twitch")
    assert m.answer == "0-1" and m.company is None and "Affirm" in m.learned
    assert memory.recall(F("How many years of professional Django experience do you have?"), "Twitch") is None


def test_company_names_are_ignored_and_history_with_a_company_stays_with_it():
    memory.remember(F("Are you currently a Twitch employee?", options=("Yes", "No")), "No", "Twitch")
    # "current employee" answers hold for one company only
    assert memory.recall(F("Are you currently a Twitch employee?", options=("Yes", "No")), "Twitch").answer == "No"
    assert memory.recall(F("Are you currently a Figma employee?", options=("Yes", "No")), "Figma") is None
    memory.remember(F("Why do you want to join Twitch? (one word)", "text", ()), "Streaming", "Twitch")
    assert memory.normalize("Why do you want to join Twitch?", "Twitch") == "why do you want to join <company>"


def test_never_stores_legal_notices_or_essays_or_files():
    assert memory.remember(F("Please read the arbitration agreement below", options=("I agree",)), "I agree",
                           "Acme") is None
    assert memory.remember(F("Why Acme?", "textarea", ()), "x" * 300, "Acme") is None
    assert memory.remember(F("Resume", "file", ()), "/r.pdf", "Acme") is None
    assert memory.load() == []


def test_a_dropdown_reuses_an_answer_only_when_the_same_option_exists():
    memory.remember(F("Which office do you prefer?", options=("New York", "Remote")), "Remote", "Acme")
    assert memory.recall(F("Which office do you prefer?", options=("Remote - US", "NYC")), "Globex") is None
    assert memory.recall(F("Which office do you prefer?", options=("remote", "NYC")), "Globex").answer == "Remote"


def test_updating_an_answer_replaces_it_and_the_file_is_editable_yaml():
    memory.remember(F("Preferred office?", "text", ()), "Remote", "Acme")
    memory.remember(F("Preferred office?", "text", ()), "Pittsburgh", "Globex")
    items = memory.load()
    assert [m.answer for m in items] == ["Pittsburgh"]
    assert memory.path().read_text().startswith("# Answers you gave on application forms")


def test_plan_uses_memory_only_where_the_rules_have_no_answer(monkeypatch):
    monkeypatch.setattr(answers, "load_user", lambda: ({"name": {"first": "Jane", "last": "Doe"}}, {
        "work_authorization": {"require_sponsorship_now": "No", "require_sponsorship_future": "Yes"}}))
    form = forms.parse_ashby(json.loads((FIX / "ashby_form_quora.json").read_text()), "https://x")
    lang = next(f for f in form.fields if f.label.startswith("What is your preferred programming language"))
    sponsor = next(f for f in form.fields if f.label.startswith("Will you now or in the future"))
    memory.remember(sponsor, "No", "Quora")  # a stale or wrong memory must not beat answers.yaml
    memory.remember(lang, "Java", "Quora")
    plan = {a.field.label: a for a in answers.plan(form, None, "Quora")}
    assert plan[sponsor.label].value == "Yes" and plan[sponsor.label].source == "answers"
    assert plan[lang.label].source == "rule"  # the Python rule exists, so memory isn't consulted
    agree = next(a for k, a in plan.items() if k.startswith("I understand"))
    assert agree.source == "needs_you"
