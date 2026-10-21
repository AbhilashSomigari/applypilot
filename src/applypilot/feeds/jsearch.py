"""Google for Jobs through the JSearch API (OpenWeb Ninja; also sold on RapidAPI).

Google for Jobs indexes LinkedIn, Indeed, Glassdoor, ZipRecruiter, Dice and employers' own career
pages. Each result lists its apply options; the runner applies on the most direct one (the employer's
ATS), and falls back to finding the posting on the company's ATS board by company + title when every
option is an aggregator.

The quota is per month (free: 200 requests), so each query is one feed, paced by
`JSearch.interval_s()` to stay under it.
"""

from __future__ import annotations

import logging

import httpx

from applypilot.config import JSearch as JSearchSettings
from applypilot.feeds.base import FeedResult, Lead

log = logging.getLogger("applypilot.feeds")

PROVIDERS = {
    # url, header carrying the key, extra headers
    "rapidapi": (
        "https://jsearch.p.rapidapi.com/search", "X-RapidAPI-Key", {"X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    ),
    "openwebninja": ("https://api.openwebninja.com/jsearch/search", "x-api-key", {}),
}
# raw fields worth keeping (the description is stored on the job itself)
_KEEP = ("job_publisher", "job_employment_type", "job_apply_is_direct", "apply_options", "job_google_link",
         "employer_website", "job_highlights", "job_min_salary", "job_max_salary", "job_salary_period")


def to_lead(j: dict) -> Lead | None:
    if not (j.get("job_id") and j.get("job_title") and j.get("employer_name")):
        return None
    options = j.get("apply_options") or []
    # direct (employer) options first; the runner still picks the best by URL shape
    urls = [o.get("apply_link") for o in options if o.get("is_direct")]
    urls += [j.get("job_apply_link")] + [o.get("apply_link") for o in options if not o.get("is_direct")]
    loc = j.get("job_location") or ", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x)
    if j.get("job_is_remote") and "remote" not in loc.lower():
        loc = f"{loc} (Remote)" if loc else "Remote"
    return Lead(
        item_id=str(j["job_id"]),
        company=j["employer_name"].strip(),
        title=j["job_title"].strip(),
        urls=list(dict.fromkeys(u for u in urls if u)),
        location=loc,
        country=j.get("job_country"),
        posted_at=j.get("job_posted_at_timestamp"),
        website=j.get("employer_website"),
        description=j.get("job_description") or "",
        raw={k: j[k] for k in _KEEP if k in j},
    )


class JSearch:
    jobs = True
    label = "Google Jobs"

    def __init__(self, query: str, cfg: JSearchSettings) -> None:
        self.query = query
        self.cfg = cfg
        self.name = f"jsearch:{query}"
        self.interval_s = cfg.interval_s()

    async def fetch(self, client: httpx.AsyncClient, etag: str | None, cursor: str | None) -> FeedResult:
        url, key_header, extra = PROVIDERS[self.cfg.provider]
        params = {
            "query": self.query,
            "page": 1,
            "num_pages": self.cfg.num_pages,
            "country": self.cfg.country,
            # "today" is nearly empty for most queries (live 2026-10-20: 0 for 3 of 4 defaults, 3 results over
            # 3 days), so only fast (paid-tier) pacing uses it; overlapping windows are deduped by job_id
            "date_posted": "today" if self.interval_s <= 6 * 3600 else "3days",
        }
        if self.cfg.employment_types:
            params["employment_types"] = self.cfg.employment_types
        headers = {key_header: self.cfg.api_key, **extra}
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 404 and self.cfg.provider == "openwebninja":
            # the direct API's path isn't documented consistently (/search vs /search-v2): try the other
            r = await client.get(f"{url}-v2", params=params, headers=headers)
        remaining = r.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) < 10:
            log.warning("jsearch quota nearly used up: %s requests left this period", remaining)
        if r.status_code != 200:
            # 401/403 = bad key or not subscribed; 429 = quota or rate limit (the runner backs off)
            return FeedResult(r.status_code, error=r.text[:200])
        data = r.json()
        if data.get("status") not in (None, "OK"):
            return FeedResult(502, error=str(data.get("error") or data.get("status"))[:200])
        return FeedResult(200, leads=[lead for j in data.get("data") or [] if (lead := to_lead(j))])
