"""Runtime settings, loaded from `$APPLYPILOT_HOME/config.yaml` (default `~/.applypilot`)."""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

USER_AGENT = "ApplyPilot/0.1 (personal job-search tool; polls public job-board APIs)"


def home() -> Path:
    return Path(os.environ.get("APPLYPILOT_HOME", Path.home() / ".applypilot")).expanduser()


class Tiers(BaseModel):
    # seconds between polls; jitter is added by the poller
    hot: int = 150
    warm: int = 900
    cold: int = 3 * 3600


class JSearch(BaseModel):
    """Google for Jobs through the JSearch API (OpenWeb Ninja, sold via RapidAPI)."""

    # secret: set with `applypilot feeds set-key jsearch` (written to this file, mode 600); never commit
    api_key: str = ""
    provider: str = "rapidapi"  # rapidapi | openwebninja
    queries: list[str] = Field(
        default_factory=lambda: [
            "software engineer new grad",
            "entry level software engineer",
            "machine learning engineer entry level",
            "data scientist entry level",
        ]
    )
    # the plan's monthly request quota (free = 200, Pro = 10,000); polling is paced to stay under it
    monthly_requests: int = 200
    num_pages: int = 1  # 10 results per page, each page costs one request
    country: str = "us"
    employment_types: str = "FULLTIME"
    min_interval: int = 900

    def interval_s(self) -> int:
        """Seconds between fetches of each query so all queries together use ~90% of the quota."""
        per_month = max(1, int(self.monthly_requests * 0.9))
        calls = max(1, len(self.queries)) * max(1, self.num_pages)
        return max(self.min_interval, int(30 * 86400 * calls / per_month))


class Feeds(BaseModel):
    # GitHub repos that publish .github/scripts/listings.json (the Simplify format)
    github_lists: list[str] = Field(
        default_factory=lambda: ["SimplifyJobs/New-Grad-Positions", "vanshb03/New-Grad-2026"]
    )
    # raw.githubusercontent caches for 300 s; a changed Simplify file is ~2 MB gzipped
    github_interval: int = 300
    hn: bool = True
    hn_interval: int = 1800
    jsearch: JSearch = Field(default_factory=JSearch)
    # job-alert emails, once `applypilot gmail login` has run; reading is free (Gmail API quota is per user)
    gmail: bool = True
    gmail_interval: int = 120
    # the Google Cloud app is in Testing: the sign-in lasts 7 days, so push a reminder on day 6
    gmail_testing: bool = True


class Tailor(BaseModel):
    """Resume variants and headless tailoring through Claude Code's /resumetailor skill."""

    resume_dir: str = "~/Documents/Resume"  # holds the master, CLAUDE.md fact base, variants/ and tailored/
    scorer: str = "~/.claude/skills/resumetailor/scripts/ats_score.py"
    claude_bin: str = "claude"
    timeout_s: int = 1800
    parallel: int = 2  # concurrent claude -p sessions
    # local repo copies the skill's repo sweep reads for verified results (outside resume_dir, so granted with
    # --add-dir); personal paths belong in ~/.applypilot/config.yaml
    repo_dirs: list[str] = Field(default_factory=list)


class Apply(BaseModel):
    # recruiters notice a spray pattern; the plan caps applications per company per 30 days
    max_per_company_30d: int = 3
    confirm_timeout_s: int = 180  # after Submit: time to solve a CAPTCHA and reach the confirmation page
    # draft written answers from the resume fact base (headless Claude Code, no tools); always reviewed by the user
    drafts: bool = True


class Prepare(BaseModel):
    """Background preparation of applications (form read, answers planned, essays drafted). Sends nothing."""

    enabled: bool = True
    min_score: float = 60
    max_age_days: float = 3
    per_cycle: int = 3
    interval_s: int = 300
    max_drafted_per_day: int = 15  # each drafted form is one headless Claude call (uses Claude usage)


class Approval(BaseModel):
    """One-tap approval from the phone (ntfy action button → a second private topic the Mac listens to)."""

    enabled: bool = False  # `applypilot approvals enable`
    topic: str = ""  # secret, like ntfy_topic: the phone posts approvals here
    secret: str = ""  # signs each Apply button (HMAC), so knowing the topic isn't enough
    min_supervised: int = 5  # confirmed applications through `apply` at the Mac before phone approvals switch on
    max_offers_per_day: int = 10
    token_ttl_h: int = 24


class Digest(BaseModel):
    """The daily summary pushed to the phone (and saved under ~/.applypilot/digests/)."""

    enabled: bool = True
    hour: int = 8  # local time
    response_window_days: int = 60  # watch Gmail for replies to applications this recent


class Settings(BaseModel):
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    mac_notifications: bool = True
    tiers: Tiers = Field(default_factory=Tiers)
    feeds: Feeds = Field(default_factory=Feeds)
    tailor: Tailor = Field(default_factory=Tailor)
    apply: Apply = Field(default_factory=Apply)
    prepare: Prepare = Field(default_factory=Prepare)
    approval: Approval = Field(default_factory=Approval)
    digest: Digest = Field(default_factory=Digest)
    max_concurrency: int = 24
    per_host_concurrency: int = 6
    timeout_s: float = 20.0
    # jobs posted within this many hours are still worth a push on a board's first scan
    fresh_hours: int = 48
    # boards with at least this many FY H-1B approvals start in the hot tier
    hot_h1b_threshold: int = 100

    @property
    def db_path(self) -> Path:
        return home() / "applypilot.db"


def config_path() -> Path:
    return home() / "config.yaml"


def write_default_config() -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # an unguessable topic: anyone who knows it can read the pushes
        data = Settings(ntfy_topic=f"applypilot-{secrets.token_urlsafe(12)}").model_dump()
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


@lru_cache
def get_settings() -> Settings:
    path = config_path()
    if path.exists():
        return Settings.model_validate(yaml.safe_load(path.read_text()) or {})
    return Settings()
