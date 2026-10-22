import pytest

from applypilot.apply.answers import answer
from applypilot.apply.forms import Field

PROFILE = {"name": {"first": "Jane Ann", "last": "Doe", "preferred": "Jane"}, "email": "jane@example.com",
           "phone": "555-555-0100", "location": {"city": "Morgantown", "state": "WV"},
           "links": {"linkedin": "https://linkedin.com/in/jane", "github": "https://github.com/jane",
                     "portfolio": "https://jane.dev"},
           "education": [{"degree": "M.S. Computer Science", "school": "West Virginia University", "end": "2099-12"}]}
ANSWERS = {"work_authorization": {"authorized_to_work_in_us": "Yes", "require_sponsorship_now": "No",
                                  "require_sponsorship_future": "Yes", "visa_status": "F-1 student (OPT/STEM OPT)"},
           "availability": {"earliest_start": "January 2027", "graduation": "December 2026",
                            "willing_to_relocate": "Yes", "remote_hybrid_onsite": "Any"},
           "compensation": {"expected_salary": "Open"},
           "misc": {"how_did_you_hear": "Company careers page", "previously_employed_here": "No", "over_18": "Yes",
                    "pronouns": "", "most_recent_employer": ""}}
YN = ["Yes", "No"]


def ask(label, kind="select", options=None, required=True, section=""):
    a = answer(Field("q", label, kind, required, options if options is not None else YN, section=section),
               PROFILE, ANSWERS, None)
    return a.value, a.source


@pytest.mark.parametrize("label, kind, options, want", [
    # sponsorship: "now" vs "now or in the future", whatever the wording
    ("Do you require visa sponsorship?", "select", YN, "No"),
    ("Will you now or in the future require employment visa sponsorship?", "select", YN, "Yes"),
    ("Now or in the future will you need immigration assistance?", "select", YN, "Yes"),
    ("Will you require sponsorship for employment visa status now or in the future?", "select", YN, "Yes"),
    ("Will you in the future require authorization to legally work in the United States?", "boolean", [], "Yes"),
    ("Will you require immigration sponsorship at any point in the future?", "select", YN, "Yes"),
    ("If yes, please enter visa type", "select", ["H1B", "TN", "F1"], None),  # a follow-up: yours
    ("Are you legally authorized to work in the United States?", "select", YN, "Yes"),
    # real option wording seen on live forms (2026-10-22)
    ("Have you previously been employed at Affirm for any length of time?", "select",
     ["I have previously been employed at Affirm", "I have not previously been employed at Affirm"],
     "I have not previously been employed at Affirm"),
    ("Have you worked at Snowflake in the past in a Full-time, Part-time, contractor or intern capacity?", "boolean",
     [], "No"),
    ("How did you first learn about us?", "multiselect", ["Campus Career Fair", "Company Website", "Referral"],
     ["Company Website"]),
    ("When is your expected graduation date? (Select the closest match):", "select",
     ["Spring 2026 (January - June)", "Summer 2026 (July - September)", "Fall 2026 (October - December)"],
     "Fall 2026 (October - December)"),
    ("Which U.S. State or Canadian Province do you reside in?", "select", ["Washington", "West Virginia"],
     "West Virginia"),
    ("Preferred First & Last Name", "text", [], "Jane Doe"),
    ("Preferred First Name", "text", [], "Jane"),
    ("Legal First Name", "text", [], "Jane Ann"),
])
def test_answers_from_the_users_files(label, kind, options, want):
    assert ask(label, kind, options)[0] == want


@pytest.mark.parametrize("label, kind, options", [
    ("Are you able to relocate to NYC?", "select", ["I am local to the NYC area", "I am willing to relocate"]),
    ("What is the address from which you plan on working? If you would need to relocate, please share", "text", []),
    ("Are you eligible to live and work in South Africa or would you need sponsorship?", "text", []),
    ("What is your expected annual salary in ZAR?", "text", []),
    ("If you answered yes to visa support, regarding CPT, please confirm", "text", []),
    ("How did you hear about X, and what do you know about us?", "text", []),
    ("If offered employment, would you be legally eligible to begin employment immediately?", "select", YN),
    ("Please read the arbitration agreement below", "select", ["I agree"]),
    ("We use Metaview, an AI notetaking tool, to record interviews. Do you consent?", "select", YN),
    ("Export Control: what is your country of citizenship?", "text", []),
    ("Have you ever interviewed at Anthropic before?", "select", YN),
    ("Why Anthropic?", "textarea", []),
    ("Pronouns", "text", []),  # misc.pronouns is empty: never guessed
    ("Who is your current (or most recent) employer?", "text", []),
    ("Are you actively enrolled in a University Program? If so, which degree", "select", ["Bachelor's", "Master's"]),
    ("During which season(s) are you available to participate in the X University program?", "multiselect",
     ["Spring", "Summer"]),
])
def test_needs_the_user(label, kind, options):
    assert ask(label, kind, options) == (None, "needs_you")


def test_location_rule_ignores_capacity_and_school_rule_ignores_dropdowns():
    assert ask("Location", "location", [])[0] == "Morgantown, WV"
    assert ask("School Name", "education", [])[0] == "West Virginia University"
    assert ask("Redact age-identifying information from university materials", "multiselect", ["OK"],
               required=False) == (None, "skip")


def test_eeo_declines_and_optional_unknowns_stay_blank():
    assert ask("Gender", "select", ["Male", "Female", "Decline To Self Identify"], False, "eeo")[0] == \
        "Decline To Self Identify"
    assert ask("Anything else you'd like to share?", "textarea", [], required=False) == (None, "skip")


def test_name_questions_in_text_boxes():
    # Lever asks this as a multi-line card question; the last-name rule must not answer it
    label = "What is your preferred first name and last name? If your legal first and last name is your preferred"
    assert ask(label, "textarea", [])[0] == "Jane Doe"
    assert ask("Legal Last Name", "text", [])[0] == "Doe"
    assert ask("Full Name", "text", [])[0] == "Jane Ann Doe"


EEO_SELF_ID = {"gender": "male", "race_ethnicity": "south asian", "veteran_status": "not a veteran",
               "disability": "no disabilities"}


@pytest.mark.parametrize("label, kind, options, want", [
    # Ashby (Quora, real options)
    ("How would you describe your gender identity?", "select", ["Male", "Female", "Non Binary", "I prefer not to say"],
     "Male"),
    ("What race and/or ethnic identities do you identify with?", "multiselect",
     ["East Asian", "Hispanic or Latinx", "South Asian", "I prefer not to say"], ["South Asian"]),
    ("US candidates only: Are you a veteran/have you served in the military?", "select",
     ["Yes", "No", "Not applicable", "I prefer not to say"], "No"),
    ("US candidates only: Do you live with a disability?", "select",
     ["Yes", "No", "Not applicable", "I prefer not to say"], "No"),
    # Greenhouse EEOC block (real options)
    ("VeteranStatus", "select", ["I don't wish to answer", "I identify as one or more of the classifications of a "
                                 "protected veteran", "I am not a protected veteran"], "I am not a protected veteran"),
    ("Are you Hispanic/Latino?", "select", ["Yes", "No", "Decline To Self Identify"], "No"),
    # Lever-style wording
    ("Race", "select", ["Hispanic or Latino", "Asian (Not Hispanic or Latino)", "Decline to self-identify"],
     "Asian (Not Hispanic or Latino)"),
    ("Veteran", "select", ["I am a veteran", "I am not a veteran", "Decline to self-identify"], "I am not a veteran"),
    ("Gender", "select", ["Female", "Male", "Decline to self-identify"], "Male"),
])
def test_eeo_follows_the_answers_file(label, kind, options, want):
    ans = {**ANSWERS, "eeo": EEO_SELF_ID}
    a = answer(Field("q", label, kind, False, options, section="eeo"), PROFILE, ans, None)
    assert a.value == want and a.source == "answers"


def test_eeo_without_a_matching_option_is_left_blank_not_declined():
    ans = {**ANSWERS, "eeo": EEO_SELF_ID}
    a = answer(Field("q", "Gender", "select", False, ["Woman", "Non-binary", "Prefer not to say"], section="eeo"),
               PROFILE, ans, None)
    assert (a.value, a.source) == (None, "skip")
    declining = {**ANSWERS, "eeo": {"gender": "Decline to self-identify"}}
    a = answer(Field("q", "Gender", "select", False, ["Male", "Decline to self-identify"], section="eeo"),
               PROFILE, declining, None)
    assert a.value == "Decline to self-identify"
