import json

from conftest import FIX

from applypilot.apply import answers, forms


def test_greenhouse_questions_and_eeoc_block():
    form = forms.parse_greenhouse(json.loads((FIX / "gh_questions_anthropic.json").read_text()), "https://x")
    by_key = {f.key: f for f in form.fields}
    assert by_key["first_name"].required and by_key["resume"].kind == "file" and "resume_text" not in by_key
    sponsor = next(f for f in form.fields if f.label.startswith("Will you now or will you in the future"))
    assert sponsor.kind == "select" and sponsor.options == ["Yes", "No"] and set(sponsor.values) == {"Yes", "No"}
    vet = by_key["veteran_status"]
    assert vet.section == "eeo" and "I don't wish to answer" in vet.options


def test_lever_standard_fields_cards_and_captcha():
    form = forms.parse_lever((FIX / "lever_apply_zoox.html").read_text(), "https://jobs.lever.co/zoox/x/apply")
    keys = [f.key for f in form.fields]
    assert keys[:4] == ["resume", "name", "pronouns", "email"] and form.captcha == "hcaptcha"
    assert {f.key: f.required for f in form.fields}["email"] and not {f.key: f.required for f in form.fields}["org"]
    cards = [f for f in form.fields if f.key.startswith("cards[")]
    assert any("Sponsorship" in f.label for f in cards) and all(f.key.endswith("]") for f in cards)
    assert any(f.key == "eeo[gender]" and f.section == "eeo" and f.options for f in form.fields)


def test_ashby_fields_types_and_required():
    form = forms.parse_ashby(json.loads((FIX / "ashby_form_quora.json").read_text()), "https://x")
    kinds = {f.label: f.kind for f in form.fields}
    assert kinds["Resume"] == "file" and kinds["School Name"] == "education" and kinds["Email"] == "email"
    degree = next(f for f in form.fields if f.label == "Degree")
    assert degree.required and "Master's Degree" in degree.options
    assert next(f for f in form.fields if f.label.startswith("How would you describe your gender")).section == "eeo"


def test_real_forms_plan_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(answers, "load_user", lambda: ({
        "name": {"first": "Jane", "last": "Doe"}, "email": "j@example.com", "phone": "555-555-0100",
        "location": {"city": "Pittsburgh", "state": "PA"}, "links": {"linkedin": "https://linkedin.com/in/j"},
        "education": [{"degree": "M.S. Computer Science", "school": "Example University", "end": "2026-12"}]},
        {"work_authorization": {"authorized_to_work_in_us": "Yes", "require_sponsorship_now": "No",
                                "require_sponsorship_future": "Yes"}, "misc": {}}))
    form = forms.parse_ashby(json.loads((FIX / "ashby_form_quora.json").read_text()), "https://x")
    resume = tmp_path / "r.pdf"
    plan = {a.field.label: a for a in answers.plan(form, resume)}
    assert plan["Full Name"].value == "Jane Doe" and plan["Resume"].value == str(resume)
    assert plan["Degree"].value == "Master's Degree" and plan["Graduation Date or Anticipated Graduation Date"].source \
        == "needs_you"
    sponsor = next(a for k, a in plan.items() if k.startswith("Will you now or in the future require sponsorship"))
    assert sponsor.value == "Yes" and sponsor.source == "answers"
    essay = next(a for k, a in plan.items() if k.startswith("This role is about"))
    assert essay.source == "needs_you"
