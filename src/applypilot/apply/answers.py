"""Map each form field to an answer from profile.yaml / answers.yaml, or mark it as needing the user.

Nothing is guessed. A field gets an answer only from a rule backed by the user's own files. Everything
else that is required (essays, legal agreements and attestations, export-control or citizenship
questions, "have you interviewed here before", unknown questions) is `needs_you`, with the reason.
Optional fields without a rule stay blank.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from applypilot.apply.forms import Field, Form
from applypilot.config import home


@dataclass
class Answer:
    field: Field
    value: str | list[str] | None  # text, an option label (or labels), "Yes"/"No" for booleans, a file path
    source: str  # profile | answers | rule | resume | needs_you | skip
    note: str = ""


def load_user() -> tuple[dict, dict]:
    def read(name):
        p = home() / f"{name}.yaml"
        return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}

    return read("profile"), read("answers")


def pick(options: list[str], *wanted: str) -> str | None:
    """The first option matching any wanted pattern (regexes, in priority order)."""
    for w in wanted:
        for o in options:
            if re.search(w, o, re.I):
                return o
    return None


# options must say yes/no outright ("I am local to the NYC area" is not a yes to "can you relocate?")
_YES = (r"^\s*yes\b",)
_NO = (r"^\s*no\b", r"^\s*(i have not|i haven'?t|i am not|i do not|i don'?t|i will not|i won'?t|never)\b")
_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
           "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
           "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
           "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
           "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
           "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
           "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
           "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
           "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
           "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
_DECLINE = (r"decline", r"prefer not", r"don.?t wish", r"do not wish", r"not to (say|disclose|answer)",
            r"choose not", r"rather not")
# answering these takes a legal or factual judgment only the user can make
_LEGAL = re.compile(r"arbitrat|agree|acknowledg|consent|certif|attest|i understand|privacy (notice|policy)|ai policy"
                    r"|notetak|record(ing|s)? (of )?(your |the )?interview|information policy"
                    r"|terms|non-?compete|background check authoriz", re.I)
# questions about working in another country, and follow-ups whose answer depends on an earlier one
_FOREIGN = re.compile(r"\b(canada|south africa|cape town|united kingdom|\buk\b|london|india|germany|ireland|singapore"
                      r"|australia|mexico|brazil|zar|gbp|eur|cad|inr)\b", re.I)
_FOLLOW_UP = re.compile(r"^\s*(if (yes|so|no|applicable|you (answered|selected|chose|checked))|please (explain|specify|"
                        r"elaborate|provide (more|additional) detail))", re.I)
_SENSITIVE = re.compile(r"export (control|compliance)|deemed export|u\.?s\.? person|citizenship"
                        r"|country of (citizenship|birth)|nationality|security clearance"
                        r"|interviewed|applied (here|to|with) .*before|previously applied|referred by|referral", re.I)


def _yes_no(f: Field, yes: bool, src: str, note: str = "") -> Answer:
    if f.kind in ("boolean", "text", "textarea"):
        return Answer(f, "Yes" if yes else "No", src, note)
    opt = pick(f.options, *(_YES if yes else _NO))
    if opt is None:
        return Answer(f, None, "needs_you", f"no {'Yes' if yes else 'No'} option among {f.options[:4]}")
    return Answer(f, opt, src, note)


def _text_or_option(f: Field, text: str, src: str, *patterns: str) -> Answer:
    if f.kind in ("select", "multiselect"):
        opt = pick(f.options, *(patterns or (re.escape(text),)))
        if opt is None:
            return Answer(f, None, "needs_you" if f.required else "skip", f"no option for {text!r}")
        return Answer(f, [opt] if f.kind == "multiselect" else opt, src)
    return Answer(f, text, src)


def answer(f: Field, profile: dict, ans: dict, resume: Path | None) -> Answer:
    lab = f.label.lower()
    wa, av, misc = ans.get("work_authorization", {}), ans.get("availability", {}), ans.get("misc", {})
    name, loc, links = profile.get("name", {}), profile.get("location", {}), profile.get("links", {})
    edu = (profile.get("education") or [{}])[0]

    if f.kind == "file":
        if re.search(r"resume|cv\b", lab) or f.key in ("resume", "_systemfield_resume"):
            return Answer(f, str(resume) if resume else None, "resume" if resume else "needs_you",
                          "" if resume else "no resume PDF found")
        return Answer(f, None, "needs_you" if f.required else "skip", "file upload (e.g. cover letter)")
    if f.section == "eeo" or re.search(r"\b(gender|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation"
                                       r"|transgender)\b", lab):
        opt = pick(f.options, *_DECLINE)
        if opt:
            return Answer(f, [opt] if f.kind == "multiselect" else opt, "answers", "voluntary self-ID: decline")
        return Answer(f, None, "needs_you" if f.required else "skip", "no decline option")
    if _LEGAL.search(lab):
        return Answer(f, None, "needs_you", "legal agreement / attestation: read and decide")
    if _SENSITIVE.search(lab):
        return Answer(f, None, "needs_you", "factual question only you can answer")
    if _FOREIGN.search(lab):
        return Answer(f, None, "needs_you" if f.required else "skip", "about another country")
    if re.search(r"\?.*\b(and|also) (what|why|how|tell|describe)\b|\band (what|why) (do|did|are|is)\b", lab):
        return Answer(f, None, "needs_you" if f.required else "skip", "written answer (two-part question)")
    if _FOLLOW_UP.search(lab):
        return Answer(f, None, "needs_you" if f.required else "skip", "follow-up to another answer")
    if re.search(r"intend to work|plan (on|to) work|work from|address from which", lab):
        return Answer(f, None, "needs_you" if f.required else "skip", "where you'll work (you're relocating)")

    if re.search(r"sponsor|immigration (assistance|support)|visa (assistance|support)|work (visa|permit)"
                 r"|require (work |employment )?authori[sz]ation", lab):
        if re.search(r"(eligib|authori[sz]ed).*\bor\b.*sponsor", lab):
            return Answer(f, None, "needs_you", "two questions in one (eligible OR needs sponsorship)")
        future = bool(re.search(r"future|will you|ever|at any point", lab))
        want = wa.get("require_sponsorship_future" if future else "require_sponsorship_now", "")
        return _yes_no(f, want.lower() == "yes", "answers", "future sponsorship" if future else "sponsorship now")
    if re.search(r"visa (status|type)|immigration status|work authorization status|current (work )?status", lab):
        return _text_or_option(f, wa.get("visa_status", ""), "answers", r"\bopt\b", r"^f-?1\b|f-?1", r"student")
    if re.search(r"authori[sz]ed to work|legally (authori|eligible|permitted)|eligible to (work|begin)|right to work"
                 r"|authori[sz]ed to work lawfully", lab):
        if re.search(r"immediately|right now|currently authori|today", lab):
            return Answer(f, None, "needs_you", "timing: full-time work on F-1 starts with OPT after graduation")
        return _yes_no(f, wa.get("authorized_to_work_in_us", "").lower() == "yes", "answers")
    if re.search(r"eligible countr|countr(y|ies).*(eligib|authori)", lab):
        return _text_or_option(f, "United States", "answers", r"united states|^us$|usa")
    if re.search(r"relocat", lab):
        return _yes_no(f, av.get("willing_to_relocate", "").lower() == "yes", "answers")
    if re.search(r"in[- ]person|in the office|on[- ]?site|hybrid|commute|in office attendance", lab) \
            and f.kind in ("select", "boolean"):
        return _yes_no(f, av.get("remote_hybrid_onsite", "").lower() in ("any", "yes"), "answers")
    if re.search(r"18 years|over 18|at least 18", lab):
        return _yes_no(f, misc.get("over_18", "").lower() == "yes", "answers")
    if re.search(r"(previously|ever) (been )?(employed|worked)|worked (for|at) .*(before|in the past|any capacity)"
                 r"|have you worked (at|for)", lab):
        return _yes_no(f, misc.get("previously_employed_here", "").lower() == "yes", "answers")
    if re.search(r"start date|earliest .*start|when can you start|available to start", lab):
        return _text_or_option(f, av.get("earliest_start", ""), "answers", r"january 2027|jan(uary)?\.? 2027")
    if re.search(r"salary|compensation|pay expectation", lab):
        return _text_or_option(f, ans.get("compensation", {}).get("expected_salary", ""), "answers", r"^$a")
    if re.search(r"how did you (first )?(hear|find|learn|discover|come across)|where did you (hear|find|learn)"
                 r"|^source", lab):
        return _text_or_option(f, misc.get("how_did_you_hear", ""), "answers",
                               r"career(s)? (page|site|website)|careers?\)?$|company('s)? (career|web)",
                               r"company website|^website$|official website")

    if re.search(r"preferred\b.*\bname", lab) and f.kind in ("text", "textarea"):
        first = name.get("preferred") or name.get("first", "")
        only_first = "first" in lab and "last" not in lab
        return Answer(f, first if only_first else f"{first} {name.get('last', '')}".strip(), "profile")
    if f.kind not in ("text", "textarea", "email", "phone", "url") and re.search(r"\bname\b", lab):
        pass  # a dropdown or yes/no question that mentions "name" is not a name field
    elif re.search(r"first name\b.*\blast name|full name|^name$|legal name\b(?!.*\bfirst)", lab) \
            and not re.search(r"^(legal )?(first|last) name", lab):
        return Answer(f, f"{name.get('first', '')} {name.get('last', '')}".strip(), "profile")
    if re.search(r"^(legal )?first name|first name$|given name", lab):
        return Answer(f, name.get("first", ""), "profile")
    if re.search(r"last name|family name|surname", lab):
        return Answer(f, name.get("last", ""), "profile")
    if re.search(r"full name|^name$|legal name", lab) or f.key in ("name", "_systemfield_name"):
        return Answer(f, f"{name.get('first', '')} {name.get('last', '')}".strip(), "profile")
    if f.kind == "email" or re.search(r"e-?mail", lab):
        return Answer(f, profile.get("email", ""), "profile")
    if f.kind == "phone" or re.search(r"\bphone|mobile", lab):
        return Answer(f, str(profile.get("phone", "")), "profile")
    text_like = f.kind in ("text", "url", "location", "education")
    if re.search(r"linkedin", lab) and text_like:
        return Answer(f, links.get("linkedin", ""), "profile")
    if re.search(r"github", lab) and text_like:
        return Answer(f, links.get("github", ""), "profile")
    if re.search(r"website|portfolio|personal site|other (url|website)", lab) and text_like:
        return Answer(f, links.get("portfolio", ""), "profile")
    if re.search(r"\bstate\b", lab) and f.kind in ("select", "multiselect") and loc.get("state"):
        full = _STATES.get(str(loc["state"]).upper(), str(loc["state"]))
        return _text_or_option(f, full, "profile", rf"^{re.escape(full)}\b", rf"^{re.escape(str(loc['state']))}$")
    if re.search(r"currently (enrolled|a student|pursuing|attending)|are you a (current )?student", lab):
        if f.kind in ("select", "multiselect") and not pick(f.options, *_YES):
            return Answer(f, None, "needs_you" if f.required else "skip", "enrollment question with custom options")
        return _yes_no(f, str(edu.get("end", "")) >= time.strftime("%Y-%m"), "profile", "M.S. in progress")
    if f.kind == "location" or (text_like and re.search(r"current location|^location|\bcity\b"
                                                         r"|where are you (currently )?(located|based)"
                                                         r"|where do you (currently )?(reside|live)", lab)):
        return Answer(f, f"{loc.get('city', '')}, {loc.get('state', '')}".strip(", "), "profile",
                      "profile location (the resume header may differ)")
    if f.kind == "education" or (text_like and re.search(r"^(school|university|college)( name)?\b", lab)):
        return Answer(f, edu.get("school", ""), "profile")
    if re.search(r"^degree|highest (level of )?(education|degree)", lab):
        return _text_or_option(f, edu.get("degree", ""), "profile", r"master", r"m\.?s\.?")
    if re.search(r"discipline|field of study|major", lab):
        return _text_or_option(f, "Computer Science", "profile", r"computer science")
    if re.search(r"graduat", lab) and f.kind in ("date", "text", "select"):
        if f.kind == "date":
            end = str(edu.get("end", ""))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):  # a month only: picking a day would be a guess
                return Answer(f, None, "needs_you" if f.required else "skip", f"pick the exact date ({end})")
            return Answer(f, end, "profile")
        return _text_or_option(f, av.get("graduation", str(edu.get("end", ""))), "profile",
                               r"dec(ember)?\.?\s*2026", r"(fall|winter) 2026", r"2026\s*\((oct|nov|dec)",
                               r"(oct|nov|dec)[a-z]*\.? ?(-|–|to) ?dec")
    if re.search(r"\bgpa\b", lab):
        return Answer(f, str(edu.get("gpa", "")), "profile")
    for key, rx in (("pronouns", r"pronoun"), ("currently_employed", r"currently (employed|working)|current employee"),
                    ("government_official_or_relative", r"government official|close relative|family member .*employ"),
                    ("noncompete", r"non-?compete|restrictive covenant|subject to an? .*agreement"),
                    ("most_recent_employer", r"(current|most recent)(ly)? (employer|company)|most recently work"),
                    ("most_recent_title", r"(current|most recent) (job )?title")):
        if re.search(rx, lab):
            v = str(misc.get(key) or "").strip()
            if not v:
                return Answer(f, None, "needs_you" if f.required else "skip", f"set misc.{key} in answers.yaml")
            if v.lower() in ("yes", "no"):
                return _yes_no(f, v.lower() == "yes", "answers")
            return _text_or_option(f, v, "answers")
    if re.search(r"programming language", lab) and f.kind in ("select", "multiselect"):
        return _text_or_option(f, "Python", "rule", r"^python$")

    if f.kind in ("textarea",) and f.required:
        return Answer(f, None, "needs_you", "written answer (essay)")
    if f.required:
        return Answer(f, None, "needs_you", "no rule for this required question")
    return Answer(f, None, "skip")


def plan(form: Form, resume: Path | None, company: str = "") -> list[Answer]:
    """Answers for every field: rules over the user's files first, then answers they gave on earlier forms."""
    from applypilot.apply import memory

    profile, ans = load_user()
    remembered = memory.load()
    out = []
    for f in form.fields:
        a = answer(f, profile, ans, resume)
        if a.source == "needs_you" and (m := memory.recall(f, company, remembered)) is not None:
            value = [v.strip() for v in m.answer.split(",")] if f.kind == "multiselect" else m.answer
            a = Answer(f, value, "memory", f"you answered this on {m.learned}")
        out.append(a)
    return out


def resume_for(settings, row) -> Path | None:
    """The resume to attach: a per-job tailored PDF, else the best-fit variant, else the master."""
    from applypilot import tailor

    if row["tailored_pdf"] and Path(row["tailored_pdf"]).exists():
        return Path(row["tailored_pdf"])
    if row["variant"] and (p := tailor.variant_pdf(settings, row["variant"])).exists():
        return p
    profile, _ = load_user()
    master = Path(str((profile.get("resume") or {}).get("master_pdf", ""))).expanduser()
    return master if master.is_file() else None
