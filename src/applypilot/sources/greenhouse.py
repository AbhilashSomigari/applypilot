"""Greenhouse public Job Board API (no key). The list endpoint omits descriptions, which keeps
polls ~10x smaller; `details` fetches the description for new jobs only."""

from __future__ import annotations

import httpx

from applypilot.models import ATS, Board, BoardResult, Job
from applypilot.sources.base import get_json, html_to_text, iso_ts

API = "https://boards-api.greenhouse.io/v1/boards"


class Greenhouse:
    ats = ATS.GREENHOUSE

    async def poll(self, client: httpx.AsyncClient, board: Board) -> BoardResult:
        status, etag, data = await get_json(client, f"{API}/{board.slug}/jobs", board.etag)
        if status != 200:
            return BoardResult(status=status, etag=etag)
        jobs = [self._job(board, j) for j in data.get("jobs", [])]
        return BoardResult(status=200, etag=etag, jobs=jobs)

    def _job(self, board: Board, j: dict) -> Job:
        jid = str(j["id"])
        loc = (j.get("location") or {}).get("name") or ""
        return Job(
            ats=self.ats,
            slug=board.slug,
            ext_id=jid,
            title=(j.get("title") or "").strip(),
            company=j.get("company_name") or board.company or board.slug,
            location=loc,
            remote="remote" in loc.lower(),
            url=j.get("absolute_url") or "",
            # the hosted form works even when absolute_url points at a custom careers site
            apply_url=f"https://job-boards.greenhouse.io/{board.slug}/jobs/{jid}",
            posted_at=iso_ts(j.get("first_published")) or iso_ts(j.get("updated_at")),
            raw=j,
        )

    async def details(self, client: httpx.AsyncClient, job: Job) -> Job:
        status, _, data = await get_json(client, f"{API}/{job.slug}/jobs/{job.ext_id}")
        if status == 200 and data:
            job.description = html_to_text(data.get("content"))
        return job

    async def exists(self, client: httpx.AsyncClient, slug: str) -> str | None:
        status, _, data = await get_json(client, f"{API}/{slug}")
        return (data or {}).get("name") or slug if status == 200 else None
