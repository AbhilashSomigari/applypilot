"""Description screening: hard drops, visa signals and knockouts, from a job's text.

Hard drops are only what the user can't legally take or isn't a real job:
- U.S. citizenship / green card / "U.S. person" (ITAR, export control) requirements;
- security clearances (required, active, or "able to obtain": all need citizenship);
- OPT "training program" / bench-sales schemes;
- 4+ years of required experience.

Everything about sponsorship is a **signal, never a drop** (OPT + STEM OPT cover ~3 years):
"no sponsorship" jobs are kept and labeled as the OPT path.

The text is cut into clauses (lines, bullets, sentences). A clause only counts when it is phrased
as a requirement: equal-opportunity boilerplate ("without regard to citizenship"), clauses that also
admit visa holders, and clauses under a "Preferred / Nice to have" header or with a softener
("a plus", "not required") don't drop a job; softened clearance and ITAR mentions become flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_YEARS = 4  # required experience at or above this drops the job (he's a new grad)

_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+(?=[A-Z(*\"'])|(?<!\d)\s[-•·*▪◦]\s+(?!\d)|;\s+|\s\|\s")
# a sentence split after these is a false one ("U.S. Citizen", "e.g. Python")
_ABBREV = re.compile(r"(\b[A-Za-z]\.){1,3}$|\b(e\.g|i\.e|etc|inc|vs|no|approx|dept|govt)\.$", re.I)
# section headers: a short line (or "Header:" / "Header -") that switches between required and preferred
_PREF_H = re.compile(r"^\W*(preferred|desired|bonus|nice[- ]to[- ]haves?|pluses|what we value|good to have"
                     r"|extra credit|it'?s a plus|ideally|you might also|even better)\b", re.I)
# searched anywhere in a short line: "Skills & Qualifications", "You'll Thrive Here If You:"
_REQ_H = re.compile(r"\b(required|requirements?|minimum|basic qualifications|must[- ]haves?|what we require"
                    r"|what you('ll| will)? need|what you bring|you have|you bring|qualifications|who you are"
                    r"|about you|looking for|you'?re a fit|eligibility|thrive|skills|experience)\b", re.I)
_RESET_H = re.compile(r"^\W*(about (us|the (company|team))|benefits|perks|compensation|salary|pay|why join"
                      r"|what we offer|equal (employment )?opportunity|eeo|our commitment|additional information"
                      r"|other information|important|please note|note|legal|export|work authorization"
                      r"|security requirements|location)\b", re.I)
_SOFT = re.compile(r"\b(preferred|a plus|is a plus|plus\b(?! \d)|nice[- ]to[- ]have|bonus|beneficial|not required"
                   r"|not necessary|desired|ideally|helpful|an advantage|advantageous|desirable|certain roles"
                   r"|some (roles|positions)|is not a requirement)\b", re.I)
# equal-opportunity and "we welcome" boilerplate, or clauses that explicitly admit non-citizens
_NOT_A_REQ = re.compile(r"regardless of|without regard|on the basis of|basis of|protected (veteran|class|status"
                        r"|characteristic)|discriminat|equal (employment )?opportunity|encouraged to apply"
                        r"|\bead\b|\bopt\b|\bf-?1\b|h-?1b (holders?|transfers?|candidates?)|visa holders?"
                        r"|non-?u\.?s\.? citizens|international candidates|green card sponsorship|will sponsor"
                        r"|consider for employment qualified applicants", re.I)
# outweighs an inherited "Preferred" section: a legal requirement stated outright
_STRONG = re.compile(r"\b(must|only|limited to|restricts?|is required|are required|required for|requires)\b", re.I)
_CUE = re.compile(r"\b(must|required?|requires|requirement|only|limited to|limit|restrict\w*|eligib\w*|need to be"
                  r"|necessary|mandatory|necessitates?|will be considered)\b", re.I)

_CITIZEN = re.compile(r"\b(u\.?\s?s\.?|united states|american)\s+(citizens?(hip)?|nationals?)\b|\bcitizenship\b"
                      r"|\bgreen card holders?\b|\b(lawful )?permanent residen(t|ts|cy)\b", re.I)
_US_PERSON = re.compile(r"\bu\.?\s?s\.?\s+persons?\b|\bitar\b|export control|export regulation|\bear\b(?=[^a-z])",
                        re.I)
_CLEARANCE = re.compile(r"\b(security clearance|clearance|ts/sci|top secret|polygraph|ci poly|full[- ]scope poly"
                        r"|public trust (clearance|position|background|investigation|eligib\w*)|privac)\b", re.I)
_CLEARANCE_NOT = re.compile(r"\b(medical|tax|customs|credit|financial) clearance\b", re.I)
# a clearance clause that reads as a requirement even without "must"/"required"
_CLEARANCE_CUE = re.compile(r"clearance( level)?\s*:|\b(active|current|hold|holding|possess|obtain|maintain|cleared"
                            r"|eligib\w*|to start|w/|with (ci|full)|level)\b", re.I)

# "STEM OPT program" alone is legitimate wording; the schemes sell training or "stop the OPT clock"
_SCAM = re.compile(r"\b(opt (training|clock|placement|candidates? training)|stop the opt|opt[- ]friendly training"
                   r"|enroll\w* in (our|the) [^.\n]{0,40}training\b"
                   r"|training fees?|pay (for|a fee|to) (the )?train\w*|guaranteed (project|placement|job)"
                   r"|bench sales|market(ing)? your (profile|resume)|we (will )?market (you|your)"
                   r"|placement (program|assistance) (for|after) training|job[- ]guarantee)\b", re.I)
_C2C = re.compile(r"\bc2c\b|corp[- ]to[- ]corp|\b1099\b", re.I)

_NO_SPONSOR = re.compile(
    r"\b(unable|not able|cannot|can'?t|can not|won'?t|will not|do not|does not|don'?t|are not|is not|not)\s+"
    r"(to\s+)?(currently\s+)?(be\s+able\s+to\s+)?((provide|offer|support|consider|accept)\w*[^.\n]{0,80}?"
    r"\b(sponsor\w*|visas?)\b|sponsor\w*)"
    r"|without (the )?(need|requiring|requirement)( for| of)?( current or future| future| employer| any| visa"
    r"| immigration)* sponsorship"
    r"|\bno (visa |h-?1b |immigration |employment )?sponsorship"
    r"|sponsorship (is |are )?(not|unavailable)|not (eligible|available) for (visa |immigration |h-?1b )?sponsorship"
    r"|(visa )?sponsorship:?\s*(no|none|not available)\b",
    re.I,
)
_SPONSOR = re.compile(
    r"\b(visa|h-?1b|immigration|green card) sponsorships? (is |are )?(available|provided|offered)"
    r"|\b(we|will|can|able to|happy to|do) (sponsor|support) (visas?|h-?1b|work visas|immigration)"
    r"|\bwe sponsor\b|sponsor(ing)? visas|visa sponsorship(: yes| for| is available| available)"
    r"|offers? (visa |h-?1b )?sponsorship|provide (visa |h-?1b |green card )sponsorship"
    r"|sponsorship:\s*yes|^\W*visa sponsorships?\W*$|\bsponsors? h-?1b|support for [^.\n]{0,30}\bvisas"
    r"|support h-?1b transfers",
    re.I,
)
_FUTURE = re.compile(r"(now|currently|present|current) or (in the )?future|future (visa )?sponsorship", re.I)
_EVERIFY = re.compile(r"\be-?\s?verify\b", re.I)

_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "twelve": 12, "fifteen": 15}
_YEARS = re.compile(rf"\b(?:minimum of |at least |over |more than |about )?{_NUM}\s*\+?\s*(?:(?:-|–|—|to)\s*{_NUM}"
                    r"\s*\+?\s*)?(?:or more\s+)?-?\s*(?:years?|yrs?)['’]?(?!\s+old)", re.I)
_EXPERIENCE = re.compile(r"\bexperience|\bexp\b|\bbackground\b|\bin (a |an )?(professional|industry|software|data"
                         r"|ml|machine|backend|full|front|devops|infra|production|engineering|research)", re.I)
_YEARS_NOISE = re.compile(r"\bwithin (the )?(last|past)?\s*\d|\bevery \d|\bpast \d|\blast \d|\bin less than"
                          r"|\bover the (past|last)|\bfor \d+ years\b(?! of)|\byears of (audio|data|history)", re.I)
_PHD = re.compile(r"\bph\.?\s?d\.?\b[^.\n]{0,40}\b(required|must|necessary)|\b(must|required to) have a ph\.?\s?d",
                  re.I)


@dataclass
class Assessment:
    drop: str | None = None  # hard-drop reason, or None
    sponsorship: str = "unknown"  # offers | no | unknown
    future_question: bool = False  # "authorized now or in the future without sponsorship" wording
    everify: bool = False
    years: int | None = None  # highest required years of experience found
    flags: list[str] = field(default_factory=list)  # soft knockouts and heads-ups
    evidence: str = ""  # the clause behind `drop`


def clauses(text: str) -> list[tuple[str, str]]:
    """(clause, section) pairs; section is "req", "pref" or "other" (company blurb, benefits, EEO)."""
    parts: list[str] = []
    for c in _SPLIT.split(text or ""):
        if c is None:
            continue
        if parts and _ABBREV.search(parts[-1]) and c[:1].isupper():
            parts[-1] += " " + c
        else:
            parts.append(c)
    out: list[tuple[str, str]] = []
    section = "other"
    for c in parts:
        c = c.strip(" \t-•*·")
        if not c:
            continue
        head = len(c) <= 60 or c.endswith(":")
        if head and _PREF_H.match(c):
            section = "pref"
        elif head and _RESET_H.match(c):
            section = "other"
        elif head and _REQ_H.search(c):
            section = "req"
        out.append((c, section))
    return out


def _min_years(clause: str) -> int | None:
    if _YEARS_NOISE.search(clause) or not _EXPERIENCE.search(clause):
        return None
    found = []
    for m in _YEARS.finditer(clause):
        n = m.group(1).lower()
        found.append(int(n) if n.isdigit() else _WORDS[n])
    # alternatives in one clause ("Bachelor's + 2 years or Master's + 0") need only the smallest
    return min(found) if found else None


def assess(text: str) -> Assessment:
    a = Assessment()
    if not text:
        return a
    text = text.replace("’", "'").replace("‘", "'")
    flags: dict[str, None] = {}
    years_req: list[int] = []
    for c, section in clauses(text):
        # a softener inside parentheses only softens that aside: "citizenship and TS eligibility (active TS preferred)"
        if c.endswith(":") and len(c) <= 40:
            continue  # a bare header ("Clearance:") states nothing; the clause after it does
        in_clause_soft = bool(_SOFT.search(re.sub(r"\([^)]*\)", "", c)))
        soft = in_clause_soft or (section == "pref" and not _STRONG.search(c))
        boiler = bool(_NOT_A_REQ.search(c))
        if a.drop is None and _SCAM.search(c):
            a.drop, a.evidence = "OPT training / bench-sales scheme", c
        if _CLEARANCE.search(c) and not _CLEARANCE_NOT.search(c) and not re.search(r"regardless|without regard", c,
                                                                                     re.I):
            if soft:
                flags["clearance preferred"] = None
            elif a.drop is None and (section == "req" or _CUE.search(c) or _CLEARANCE_CUE.search(c)):
                a.drop, a.evidence = "requires security clearance", c
        if _CITIZEN.search(c) and not boiler:
            if soft:
                flags["citizenship preferred"] = None
            elif a.drop is None and (_CUE.search(c) or section == "req"):
                a.drop, a.evidence = "requires U.S. citizenship or green card", c
        if _US_PERSON.search(c) and not (boiler and not re.search(r"u\.?\s?s\.?\s+persons?", c, re.I)):
            if soft or not _CUE.search(c):
                flags["export-control mention"] = None
            elif a.drop is None:
                a.drop, a.evidence = "U.S. persons only (ITAR / export control)", c
        if _NO_SPONSOR.search(c):
            a.sponsorship = "no"
        elif a.sponsorship == "unknown" and _SPONSOR.search(c):
            a.sponsorship = "offers"
        if _FUTURE.search(c) and re.search(r"sponsor|authoriz", c, re.I):
            a.future_question = True
        if _EVERIFY.search(c):
            a.everify = True
        if _C2C.search(c):
            flags["contract (C2C/1099)"] = None
        if _PHD.search(c) and not soft:
            flags["PhD required"] = None
        if section != "other" or _CUE.search(c) or re.search(r"\b(you have|you'?ll need|we'?re looking)", c, re.I):
            n = _min_years(c)
            if n is not None and not soft:
                years_req.append(n)
    if years_req:
        a.years = max(years_req)
        if a.drop is None and a.years >= MAX_YEARS:
            a.drop = f"requires {a.years}+ years of experience"
        elif a.years >= 2:
            flags[f"{a.years}+ years required"] = None
    a.flags = list(flags)
    return a


def label(a: Assessment, h1b_approvals: int | None = None) -> str:
    """Short visa line for a push: sponsorship stance, H-1B history, E-Verify, heads-ups."""
    parts = []
    if a.sponsorship == "offers":
        parts.append("sponsors visas")
    elif a.sponsorship == "no":
        parts.append("no sponsorship: OPT/STEM OPT path")
    if h1b_approvals:
        parts.append(f"{h1b_approvals} H-1B approvals")
    if a.everify:
        parts.append("E-Verify")
    if a.future_question:
        parts.append("asks about future sponsorship")
    parts += a.flags
    return " · ".join(parts)
