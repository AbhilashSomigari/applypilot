"""Feeds list jobs (or hiring companies) across many employers: GitHub new-grad lists, HN "Who is
hiring", and later alert emails and Google Jobs. Each poll returns leads; the runner resolves
them to ATS jobs and boards."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class Lead:
    """One posting seen on a feed."""

    item_id: str  # the feed's own id; a lead is processed once per feed
    company: str
    title: str = ""  # empty for company-level leads (an HN post covering several roles)
    # job leads: the job's links (the runner picks the most direct); HN: every link in the post
    urls: list[str] = field(default_factory=list)
    location: str = ""
    country: str | None = None  # when the feed gives one (JSearch "US")
    posted_at: float | None = None
    website: str | None = None  # company site, for probing its ATS boards
    drop_reason: str | None = None  # the feed itself marks it out of scope (e.g. citizenship required)
    description: str = ""
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class FeedResult:
    status: int  # HTTP status of the main request; 304 = unchanged
    etag: str | None = None
    cursor: str | None = None  # new position to store (None = keep the old one)
    leads: list[Lead] = field(default_factory=list)
    error: str | None = None


class Feed(Protocol):
    name: str  # stable key in the feeds table
    label: str  # short name shown in pushes ("Simplify", "HN")
    interval_s: int
    jobs: bool  # leads are single jobs (True) or hiring companies (False)

    async def fetch(self, client: httpx.AsyncClient, etag: str | None, cursor: str | None) -> FeedResult: ...
