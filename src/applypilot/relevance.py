"""Cheap title + location gate run on every new job before anything expensive.

Decides "is this an early-career software/ML/data role in the US (or US-remote)?". It deliberately
says nothing about sponsorship: "no sponsorship" jobs stay in scope (OPT/STEM OPT covers ~3 years).
Description-based hard drops (citizenship, clearance, ITAR) live in the filters stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from applypilot.models import Job

TECH = re.compile(
    r"\b(software|swe|sde|developer|programmer|machine learning|ml|ai|artificial intelligence"
    r"|data scien\w*|data engineer\w*|data analyst|analytics engineer|applied scien\w*"
    r"|research (engineer|scientist)|full[- ]?stack|back[- ]?end|front[- ]?end|platform"
    r"|infrastructure|devops|site reliability|sre|mlops|computer vision|nlp|llms?|genai|gen ai"
    r"|generative|deep learning|forward[- ]deployed|solutions engineer|member of technical staff"
    r"|cloud engineer|automation engineer|python|product engineer|web engineer)\b",
    re.I,
)
SENIOR = re.compile(
    r"\b(senior|sr|staff|principal|lead|leader|manager|director|head|vp|vice president|chief"
    r"|architect|distinguished|fellow|iii|iv|l5|l6|l7|e5|e6|e7)\b",
    re.I,
)
NOT_FULLTIME = re.compile(r"\b(intern|internship|co-?op|apprentice|apprenticeship)\b", re.I)
OTHER_FIELD = re.compile(
    r"\b(mechanical|electrical|civil|chemical|hardware|manufacturing|process engineer|field service"
    r"|sales|account executive|recruit\w*|legal|counsel|accounting|marketing|designer|nurse"
    r"|technician|mechanic|gtm|go-to-market|strateg\w*)\b",
    re.I,
)
# a hard drop (the user can't hold a clearance on OPT); the description-level check comes in Phase 3
CLEARANCE = re.compile(r"\b(cleared|clearance|ts/sci|top secret|polygraph)\b", re.I)
NEW_GRAD = re.compile(
    r"new[- ]?grad|entry[- ]level|\bjunior\b|\bjr\b|\bassociate\b|university|\bgraduate\b|early[- ]career"
    r"|\bcampus\b|\b(i|1|l3|e3|level 1)\s*$|\b(engineer|developer|scientist) i\b|\b202[67]\b",
    re.I,
)

US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC"
    " ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()
US_HINT = re.compile(
    r"united states|\busa\b|\bu\.s\.?\b|\bus\b|americas|north america"
    r"|san francisco|bay area|new york|nyc|seattle|boston|austin|chicago|los angeles|denver|atlanta"
    r"|pittsburgh|philadelphia|charlotte|cleveland|washington|palo alto|mountain view|menlo park"
    r"|sunnyvale|san jose|san diego|dallas|houston|miami|portland|salt lake|raleigh|durham|minneapolis"
    r"|detroit|columbus|nashville|phoenix|jersey city|newark|princeton|cambridge, ma|morgantown"
    r"|, (" + "|".join(US_STATES) + r")\b",
    re.I,
)
NON_US = re.compile(
    r"canada|toronto|vancouver|montreal|ontario|united kingdom|\buk\b|london|england|ireland|dublin"
    r"|germany|berlin|munich|france|paris|netherlands|amsterdam|spain|madrid|barcelona|portugal|lisbon"
    r"|poland|warsaw|india|bangalore|bengaluru|hyderabad|pune|chennai|delhi|gurgaon|noida|singapore"
    r"|australia|sydney|melbourne|japan|tokyo|korea|seoul|china|shanghai|beijing|hong kong|taiwan"
    r"|israel|tel aviv|brazil|são paulo|sao paulo|mexico|argentina|colombia|chile|emea|apac|latam"
    r"|europe|switzerland|zurich|sweden|stockholm|denmark|copenhagen|philippines|vietnam|nigeria"
    r"|kenya|south africa|uae|dubai|romania|serbia|ukraine|czech|prague|estonia|finland|norway|italy|milan"
    r"|remoto|brasil|rio de janeiro",
    re.I,
)


@dataclass
class Verdict:
    relevant: bool
    reason: str
    new_grad: bool = False


def is_us(job: Job) -> bool | None:
    """True/False when we can tell, None when the posting gives no location at all."""
    if job.country:
        if job.country.upper() in ("US", "USA", "UNITED STATES"):
            return True
        # a non-US primary country can still list US offices in the location text
        return bool(US_HINT.search(job.location))
    loc = job.location.strip()
    if not loc:
        return None
    if US_HINT.search(loc):
        return True
    if NON_US.search(loc):
        return False
    # bare "Remote", "Hybrid", "Anywhere": assume it can include the US
    return True if re.search(r"remote|anywhere|hybrid|distributed", loc, re.I) else None


def judge(job: Job) -> Verdict:
    t = job.title
    if not TECH.search(t):
        return Verdict(False, "not a software/ML/data title")
    if OTHER_FIELD.search(t):
        return Verdict(False, "different engineering field or non-eng role")
    if NOT_FULLTIME.search(t):
        return Verdict(False, "internship/co-op")
    if CLEARANCE.search(t):
        return Verdict(False, "requires security clearance")
    new_grad = bool(NEW_GRAD.search(t))
    if SENIOR.search(t) and not new_grad:
        return Verdict(False, "senior/lead/manager title")
    us = is_us(job)
    if us is False:
        return Verdict(False, f"outside US: {job.location[:60]}")
    where = "US" if us else "location unknown"
    return Verdict(True, f"{'new-grad ' if new_grad else ''}tech role, {where}", new_grad)
