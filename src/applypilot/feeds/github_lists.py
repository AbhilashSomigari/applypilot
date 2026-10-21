"""Community new-grad lists on GitHub (SimplifyJobs/New-Grad-Positions and forks of its format).

Each repo publishes `.github/scripts/listings.json`, the structured source of its README table. We
fetch it from raw.githubusercontent.com with If-None-Match, so an unchanged list costs a 304.
Simplify adds listings for every ATS, including Workday and Oracle sites we can't poll ourselves.
"""

from __future__ import annotations

import httpx

from applypilot.feeds.base import FeedResult, Lead
from applypilot.sources.base import get_json

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/.github/scripts/listings.json"
# the "sponsorship" values that are hard drops for an F-1/OPT candidate ("Does Not Offer
# Sponsorship" stays in: OPT/STEM OPT covers ~3 years)
DROP = {"U.S. Citizenship is Required": "requires US citizenship"}


class GitHubList:
    jobs = True

    def __init__(self, repo: str, interval_s: int = 300, branch: str = "dev") -> None:
        self.repo = repo
        self.branch = branch
        self.name = f"github:{repo}"
        self.label = repo.split("/")[0].removesuffix("Jobs") or repo
        self.interval_s = interval_s

    async def fetch(self, client: httpx.AsyncClient, etag: str | None, cursor: str | None) -> FeedResult:
        status, new_etag, data = await get_json(client, RAW.format(repo=self.repo, branch=self.branch), etag)
        if status != 200:
            return FeedResult(status, new_etag)
        return FeedResult(200, new_etag, leads=[lead for row in data or [] if (lead := self._lead(row))])

    def _lead(self, row: dict) -> Lead | None:
        if not (row.get("active") and row.get("is_visible", True) and row.get("url") and row.get("id")):
            return None
        return Lead(
            item_id=str(row["id"]),
            company=(row.get("company_name") or "").strip(),
            title=(row.get("title") or "").strip(),
            urls=[row["url"]],
            location=" | ".join(row.get("locations") or []),
            posted_at=row.get("date_posted"),
            drop_reason=DROP.get(row.get("sponsorship") or ""),
            raw=row,
        )
