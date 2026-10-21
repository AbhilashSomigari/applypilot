"""Shared helpers for ATS sources: conditional GETs and HTML-to-text."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Protocol

import httpx

from applypilot.models import Board, BoardResult, Job


class Source(Protocol):
    async def poll(self, client: httpx.AsyncClient, board: Board) -> BoardResult: ...

    async def details(self, client: httpx.AsyncClient, job: Job) -> Job:
        """Fill fields the list endpoint omits (e.g. the description). Default: no-op."""
        ...

    async def exists(self, client: httpx.AsyncClient, slug: str) -> str | None:
        """Return the board's display name if `slug` is a live board on this ATS, else None."""
        ...


async def get_json(
    client: httpx.AsyncClient, url: str, etag: str | None = None
) -> tuple[int, str | None, Any]:
    headers = {"If-None-Match": etag} if etag else {}
    resp = await client.get(url, headers=headers)
    new_etag = resp.headers.get("etag")
    if resp.status_code == 304:
        return 304, etag, None
    if resp.status_code != 200:
        return resp.status_code, None, None
    return 200, new_etag, resp.json()


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)\b[^>]*>", re.I)
_SPACES = re.compile(r"[ \t\xa0]+")
_BLANKS = re.compile(r"\n\s*\n+")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its HTML
    s = _BLOCK.sub("\n", s)
    s = html.unescape(_TAG.sub("", s))
    s = _SPACES.sub(" ", s)
    return _BLANKS.sub("\n\n", s).strip()


def iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
