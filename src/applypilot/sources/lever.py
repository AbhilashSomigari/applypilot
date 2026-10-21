"""Lever public Postings API (no key). Descriptions come with the list."""

from __future__ import annotations

import httpx

from applypilot.models import ATS, Board, BoardResult, Job
from applypilot.sources.base import get_json, html_to_text

API = "https://api.lever.co/v0/postings"


def _plain(p: dict) -> str:
    parts = [p.get("descriptionPlain") or ""]
    for lst in p.get("lists") or []:
        items = (lst.get("content") or "").replace("</li>", "\n").replace("<li>", "- ")
        parts.append(f"{lst.get('text', '')}\n{items}")
    parts.append(p.get("additionalPlain") or "")
    return html_to_text("\n\n".join(parts))


class Lever:
    ats = ATS.LEVER

    async def poll(self, client: httpx.AsyncClient, board: Board) -> BoardResult:
        status, etag, data = await get_json(client, f"{API}/{board.slug}?mode=json", board.etag)
        if status != 200:
            return BoardResult(status=status, etag=etag)
        return BoardResult(status=200, etag=etag, jobs=[self._job(board, p) for p in data or []])

    def _job(self, board: Board, p: dict) -> Job:
        cats = p.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        created = p.get("createdAt")
        return Job(
            ats=self.ats,
            slug=board.slug,
            ext_id=p["id"],
            title=(p.get("text") or "").strip(),
            company=board.company or board.slug,
            location=loc,
            country=p.get("country"),
            remote=(p.get("workplaceType") == "remote") or "remote" in loc.lower(),
            url=p.get("hostedUrl") or "",
            apply_url=p.get("applyUrl") or "",
            description=_plain(p),
            posted_at=created / 1000 if created else None,
            raw={k: v for k, v in p.items() if k not in ("description", "descriptionBody", "opening")},
        )

    async def details(self, client: httpx.AsyncClient, job: Job) -> Job:
        return job

    async def exists(self, client: httpx.AsyncClient, slug: str) -> str | None:
        status, _, _ = await get_json(client, f"{API}/{slug}?mode=json&limit=1")
        return slug if status == 200 else None
