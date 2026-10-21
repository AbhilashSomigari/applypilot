"""Hacker News "Ask HN: Who is hiring?" via the public Algolia HN API (adapted from ProspectPilot).

Each top-level comment is one company, conventionally `Company | Role(s) | Location | ... | URL`.
A post usually covers several roles, so leads are companies: the runner registers every ATS board
linked from the post and probes the company's name and website for the rest.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

import httpx

from applypilot.feeds.base import FeedResult, Lead

ALGOLIA = "https://hn.algolia.com/api/v1"
_HREF = re.compile(r'href="([^"]+)"')
# hosts that never name the company's own site: ATSs and hiring platforms (their own boards
# would be probed instead), social and code hosting, archives and docs
_NOT_SITE = (
    "lever.co", "greenhouse.io", "grnh.se", "ashbyhq.com", "workable.com", "myworkdayjobs.com", "smartrecruiters.com",
    "applytojob.com", "jazzhr.com", "bamboohr.com", "breezy.hr", "recruitee.com", "teamtailor.com", "jobvite.com",
    "icims.com", "rippling.com", "rippling-ats.com", "gem.com", "deel.com", "dover.com", "dover.io", "personio.de",
    "personio.com", "pinpointhq.com", "homerun.co", "join.com", "welcometothejungle.com", "wellfound.com",
    "workatastartup.com", "ycombinator.com", "builtin.com", "indeed.com", "glassdoor.com", "linkedin.com",
    "github.com", "gitlab.com", "x.com", "twitter.com", "youtube.com", "youtu.be", "medium.com", "substack.com",
    "archive.ph", "archive.org", "arxiv.org", "wikipedia.org", "google.com", "forms.gle", "notion.site", "notion.so",
    "typeform.com", "tally.so", "airtable.com", "calendly.com", "bit.ly",
)


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<p>", "\n", text)))


def _site(urls: list[str]) -> str | None:
    for u in urls:
        host = urlsplit(u).netloc.lower().removeprefix("www.")
        if "." in host and not any(host == d or host.endswith("." + d) for d in _NOT_SITE):
            return host
    return None


def company_name(first_field: str) -> str:
    """'Column (YC S20)' -> 'Column'; 'telli hiring in Eng, GTM [Berlin]' -> 'telli'."""
    name = re.sub(r"https?://\S+", "", first_field)
    name = re.split(r"\s*[(\[]|\s+(?:is\s+)?hiring\b|\s+-\s+|,", name, maxsplit=1, flags=re.I)[0]
    return name.strip(" -|,:")


def parse_post(item: dict) -> Lead | None:
    raw = item.get("comment_text") or ""
    if not raw:
        return None
    text = _strip_html(raw)
    first = text.strip().split("\n", 1)[0]
    parts = [p.strip() for p in re.split(r"\s*[|—–]\s*", first) if p.strip()]
    if not parts:
        return None
    name = company_name(parts[0])
    if not name or len(name) > 60:
        return None
    urls = list(dict.fromkeys(html.unescape(h) for h in _HREF.findall(raw)))
    locs = [p for p in parts[1:] if re.search(r"remote|onsite|hybrid|,|\b[A-Z]{2}\b", p, re.I)]
    return Lead(
        item_id=str(item["objectID"]),
        company=name,
        urls=urls,
        location=" | ".join(locs[:3]),
        posted_at=item.get("created_at_i"),
        website=_site(urls),
        raw={"text": text[:2000], "author": item.get("author")},
    )


class HackerNews:
    name = "hn"
    label = "HN"
    jobs = False

    def __init__(self, interval_s: int = 1800) -> None:
        self.interval_s = interval_s

    async def _latest_thread(self, client: httpx.AsyncClient) -> int:
        params = {"tags": "story,author_whoishiring", "hitsPerPage": 10}
        r = await client.get(f"{ALGOLIA}/search_by_date", params=params)
        r.raise_for_status()
        for hit in r.json()["hits"]:
            if str(hit.get("title", "")).startswith("Ask HN: Who is hiring?"):
                return int(hit["objectID"])
        raise LookupError("no 'Who is hiring?' thread found")

    async def fetch(self, client: httpx.AsyncClient, etag: str | None, cursor: str | None) -> FeedResult:
        # cursor = {"thread": id, "since": newest comment time seen}; a new month's thread starts over
        state = json.loads(cursor) if cursor else {}
        tid = await self._latest_thread(client)
        since = state.get("since", 0) if state.get("thread") == tid else 0
        r = await client.get(
            f"{ALGOLIA}/search_by_date",
            params={"tags": f"comment,story_{tid}", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 1000},
        )
        if r.status_code != 200:
            return FeedResult(r.status_code, error=r.text[:200])
        hits = r.json()["hits"]
        top = [h for h in hits if h.get("parent_id") == tid]
        newest = max([since, *(h.get("created_at_i") or 0 for h in hits)])
        leads = [lead for h in top if (lead := parse_post(h))]
        return FeedResult(200, cursor=json.dumps({"thread": tid, "since": newest}), leads=leads)
