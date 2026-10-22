"""Read a job's application form into one normalized shape, without a browser.

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true` (questions plus the EEOC
  compliance block).
- Lever: the public apply page's HTML; custom questions ("cards") carry their definitions in a hidden
  `cards[<id>][baseTemplate]` JSON input.
- Ashby: the hosted job page's GraphQL (`jobs.ashbyhq.com/api/non-user-graphql`, `ApiJobPosting`).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class Field:
    key: str  # what the form submits / the filler targets (GH field name, Lever input name, Ashby path)
    label: str
    kind: str  # text | textarea | email | phone | url | file | select | multiselect | boolean | date | location
    required: bool = False
    options: list[str] = field(default_factory=list)  # option labels, for select / multiselect
    values: dict[str, str] = field(default_factory=dict)  # option label -> submitted value (Greenhouse)
    section: str = ""  # "eeo" for voluntary self-identification


@dataclass
class Form:
    ats: str
    url: str  # the page a person (or the filler) applies on
    fields: list[Field]
    captcha: str | None = None  # hcaptcha / recaptcha when the page loads one


_GH_KIND = {"input_text": "text", "textarea": "textarea", "input_file": "file",
            "multi_value_single_select": "select", "multi_value_multi_select": "multiselect"}


def parse_greenhouse(data: dict, url: str) -> Form:
    fields: list[Field] = []
    for q in data.get("questions") or []:
        for f in q.get("fields") or []:
            kind = _GH_KIND.get(f.get("type"))
            if kind is None or f["name"] == "resume_text":  # hidden inputs; the paste-resume alternative
                continue
            if f["name"] == "cover_letter_text":
                continue
            opts = f.get("values") or []
            fields.append(Field(f["name"], q.get("label", "").strip(), kind, bool(q.get("required")),
                                [o["label"] for o in opts], {o["label"]: str(o["value"]) for o in opts}))
    for block in data.get("compliance") or []:
        for q in block.get("questions") or []:
            for f in q.get("fields") or []:
                opts = f.get("values") or []
                fields.append(Field(f["name"], q.get("label", f["name"]), "select", bool(q.get("required")),
                                    [o["label"] for o in opts], {o["label"]: str(o["value"]) for o in opts}, "eeo"))
    return Form("greenhouse", url, fields)


async def greenhouse(client: httpx.AsyncClient, slug: str, job_id: str, url: str) -> Form:
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
                         params={"questions": "true"})
    r.raise_for_status()
    return parse_greenhouse(r.json(), url)


_LEVER_STD = {"resume": ("Resume/CV", "file"), "name": ("Full name", "text"), "email": ("Email", "email"),
              "phone": ("Phone", "phone"), "location": ("Current location", "location"),
              "org": ("Current company", "text"), "pronouns": ("Pronouns", "text")}
_LEVER_KIND = {"text": "text", "textarea": "textarea", "multiple-choice": "select", "dropdown": "select",
               "multiple-select": "multiselect", "file-upload": "file"}


def _label_required(page: str, name: str) -> bool:
    # each standard field sits in an <li> whose label ends with "✱" when required
    m = re.search(r'<li[^>]*application-question[^>]*>(?:(?!</li>).)*?name="' + re.escape(name) + '"', page, re.S)
    return bool(m and "✱" in m.group(0))


def parse_lever(page: str, url: str) -> Form:
    fields: list[Field] = []
    names = list(dict.fromkeys(re.findall(r'<(?:input|textarea|select)[^>]*\sname="([^"]+)"', page)))
    for n in names:
        if n in _LEVER_STD:
            label, kind = _LEVER_STD[n]
            fields.append(Field(n, label, kind, _label_required(page, n)))
        elif n.startswith("urls["):
            fields.append(Field(n, n[5:-1] + " URL", "url", _label_required(page, n)))
    for tag in re.findall(r'<input[^>]*\[baseTemplate\][^>]*>', page):  # value= may come before or after name=
        name, value = re.search(r'name="([^"]+)\[baseTemplate\]"', tag), re.search(r'value="([^"]*)"', tag)
        if not (name and value):
            continue
        card, tmpl = name.group(1), json.loads(html.unescape(value.group(1)))
        for i, f in enumerate(tmpl.get("fields") or []):
            kind = _LEVER_KIND.get(f.get("type"), "text")
            fields.append(Field(f"{card}[field{i}]", (f.get("text") or "").strip(), kind, bool(f.get("required")),
                                [o.get("text", "") for o in f.get("options") or []]))
    for name in ("eeo[gender]", "eeo[race]", "eeo[veteran]", "eeo[disability]"):
        sel = re.search(r'<select[^>]*name="' + re.escape(name) + r'"[^>]*>(.*?)</select>', page, re.S)
        if sel:
            opts = [html.unescape(o).strip() for o in re.findall(r"<option[^>]*>([^<]*)</option>", sel.group(1))]
            fields.append(Field(name, name[4:-1].title(), "select", False,
                                [o for o in opts if o and not o.lower().startswith("select")], section="eeo"))
    captcha = "hcaptcha" if "h-captcha" in page else ("recaptcha" if "recaptcha" in page.lower() else None)
    return Form("lever", url, fields, captcha)


async def lever(client: httpx.AsyncClient, apply_url: str) -> Form:
    r = await client.get(apply_url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    r.raise_for_status()
    return parse_lever(r.text, apply_url)


_ASHBY_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
    " jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {"
    " id title applicationForm { sections { title fieldEntries {"
    " ... on FormFieldEntry { id field isRequired } } } } } }"
)
_ASHBY_KIND = {"String": "text", "Email": "email", "Phone": "phone", "LongText": "textarea", "File": "file",
               "ValueSelect": "select", "MultiValueSelect": "multiselect", "Boolean": "boolean", "Date": "date",
               "Location": "location", "Number": "text", "SocialLink": "url"}
_EEO_WORDS = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|transgender|pronoun", re.I)


def parse_ashby(data: dict, url: str) -> Form:
    jp = (data.get("data") or {}).get("jobPosting") or {}
    fields: list[Field] = []
    for s in (jp.get("applicationForm") or {}).get("sections") or []:
        for e in s.get("fieldEntries") or []:
            f = e.get("field") or {}
            ftype = f.get("type")
            if ftype == "EducationHistory":
                kind = "education"
            else:
                kind = _ASHBY_KIND.get(ftype, "text")
            label = (f.get("title") or "").strip()
            opts = [v.get("label", "") for v in f.get("selectableValues") or []]
            section = "eeo" if _EEO_WORDS.search(label) and not e.get("isRequired") else ""
            fields.append(Field(f.get("path") or e.get("id"), label, kind, bool(e.get("isRequired")), opts,
                                section=section))
    return Form("ashby", url, fields)


async def ashby(client: httpx.AsyncClient, slug: str, job_id: str, url: str) -> Form:
    r = await client.post("https://jobs.ashbyhq.com/api/non-user-graphql", params={"op": "ApiJobPosting"},
                          json={"operationName": "ApiJobPosting", "query": _ASHBY_QUERY,
                                "variables": {"organizationHostedJobsPageName": slug, "jobPostingId": job_id}})
    r.raise_for_status()
    return parse_ashby(r.json(), url)


async def fetch(client: httpx.AsyncClient, ats: str, slug: str, ext_id: str, apply_url: str) -> Form:
    if ats == "greenhouse":
        return await greenhouse(client, slug, ext_id, apply_url)
    if ats == "lever":
        return await lever(client, apply_url)
    if ats == "ashby":
        return await ashby(client, slug, ext_id, apply_url)
    raise ValueError(f"no form reader for {ats} (apply manually: {apply_url})")
