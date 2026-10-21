"""Push new-job alerts: ntfy (phone) and a macOS banner."""

from __future__ import annotations

import subprocess
import time

import httpx

from applypilot.config import Settings
from applypilot.models import Job


def age(posted_at: float | None, now: float | None = None) -> str:
    if not posted_at:
        return "posted ?"
    mins = max(0, int(((now or time.time()) - posted_at) // 60))
    if mins < 60:
        return f"posted {mins} min ago"
    if mins < 48 * 60:
        return f"posted {mins // 60} h ago"
    return f"posted {mins // 1440} d ago"


def _ascii(s: str) -> str:
    # HTTP headers must be latin-1; ntfy titles with em dashes etc. would fail
    return s.encode("latin-1", "replace").decode("latin-1")


async def push(client: httpx.AsyncClient, settings: Settings, job: Job, reason: str) -> None:
    title = f"{job.company}: {job.title}"
    body = f"{job.location or 'location n/a'} · {age(job.posted_at)} · {reason}"
    if settings.ntfy_topic:
        try:
            await client.post(
                f"{settings.ntfy_server}/{settings.ntfy_topic}",
                content=body.encode(),
                headers={
                    "Title": _ascii(title),
                    "Click": job.apply_url or job.url,
                    "Priority": "high" if "new-grad" in reason else "default",
                    "Tags": "briefcase",
                },
            )
        except httpx.HTTPError:
            pass  # a lost push must never stop polling
    if settings.mac_notifications:
        text, head = _applescript_str(body), _applescript_str(title)
        script = f'display notification {text} with title {head} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


async def message(client: httpx.AsyncClient, settings: Settings, title: str, body: str,
                  priority: str = "default") -> None:
    """A plain notice (not a job), e.g. "Gmail sign-in expired"."""
    if settings.ntfy_topic:
        try:
            await client.post(f"{settings.ntfy_server}/{settings.ntfy_topic}", content=body.encode(),
                              headers={"Title": _ascii(title), "Priority": priority, "Tags": "warning"})
        except httpx.HTTPError:
            pass
    if settings.mac_notifications:
        script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
