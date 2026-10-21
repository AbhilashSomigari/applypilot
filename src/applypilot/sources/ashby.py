"""Ashby public Posting API (no key). One call returns every listed job with descriptions."""

from __future__ import annotations

import httpx

from applypilot.models import ATS, Board, BoardResult, Job
from applypilot.sources.base import get_json, iso_ts

API = "https://api.ashbyhq.com/posting-api/job-board"


def _country(addr: dict | None) -> str | None:
    return ((addr or {}).get("postalAddress") or {}).get("addressCountry")


class Ashby:
    ats = ATS.ASHBY

    async def poll(self, client: httpx.AsyncClient, board: Board) -> BoardResult:
        url = f"{API}/{board.slug}?includeCompensation=true"
        status, etag, data = await get_json(client, url, board.etag)
        if status != 200:
            return BoardResult(status=status, etag=etag)
        jobs = [self._job(board, j) for j in (data or {}).get("jobs", []) if j.get("isListed", True)]
        return BoardResult(status=200, etag=etag, jobs=jobs)

    def _job(self, board: Board, j: dict) -> Job:
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        countries = {_country(j.get("address"))} | {
            _country(s.get("address")) for s in j.get("secondaryLocations") or []
        }
        countries.discard(None)
        us = next((c for c in countries if c in ("USA", "United States", "US")), None)
        comp = (j.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
        desc = j.get("descriptionPlain") or ""
        return Job(
            ats=self.ats,
            slug=board.slug,
            ext_id=j["id"],
            title=(j.get("title") or "").strip(),
            company=board.company or board.slug,
            location=" | ".join(x for x in locs if x),
            country="US" if us else (next(iter(countries)) if len(countries) == 1 else None),
            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            url=j.get("jobUrl") or "",
            apply_url=j.get("applyUrl") or "",
            description=f"Compensation: {comp}\n\n{desc}" if comp else desc,
            posted_at=iso_ts(j.get("publishedAt")),
            raw={k: v for k, v in j.items() if k not in ("descriptionHtml", "descriptionPlain")},
        )

    async def details(self, client: httpx.AsyncClient, job: Job) -> Job:
        return job

    async def exists(self, client: httpx.AsyncClient, slug: str) -> str | None:
        status, _, _ = await get_json(client, f"{API}/{slug}")
        return slug if status == 200 else None
