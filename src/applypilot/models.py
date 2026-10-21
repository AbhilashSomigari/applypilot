from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ATS(StrEnum):
    """ATSs whose boards we poll. Jobs found through feeds can also carry other kinds
    (workday, smartrecruiters, workable, other) as a plain string."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"


@dataclass
class Board:
    ats: ATS
    slug: str
    company: str = ""
    tier: str = "warm"
    etag: str | None = None
    baseline_done: bool = False


@dataclass
class Job:
    ats: ATS | str
    slug: str
    ext_id: str
    title: str
    company: str
    location: str = ""
    country: str | None = None  # ISO-ish country when the ATS gives one
    remote: bool = False
    url: str = ""
    apply_url: str = ""
    description: str = ""  # plain text; may be filled later for list-only sources
    posted_at: float | None = None  # unix seconds, from the ATS
    source: str = "ats"  # "ats" (board poll) or the feed that found it first
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.slug}:{self.ext_id}"


@dataclass
class BoardResult:
    """Outcome of polling one board."""

    status: int  # HTTP status; 304 = unchanged
    etag: str | None = None
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
