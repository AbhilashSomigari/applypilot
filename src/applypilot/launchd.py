"""Per-user launchd agents that start at login and restart if they die: the poller (boards, feeds,
preparation, digest) and the web dashboard."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from applypilot.config import home

LABEL = "com.applypilot.poller"
WEB_LABEL = "com.applypilot.web"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
WEB_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{WEB_LABEL}.plist"
REPO = Path(__file__).resolve().parents[2]


def plist(label: str = LABEL, command: tuple[str, ...] = ("run",), log: str = "run.log") -> dict:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    args = [uv, "run", "--project", str(REPO), "applypilot", *command]
    if label == LABEL:
        # caffeinate -i keeps the Mac from idle-sleeping while the poller runs (lid-closed sleep still wins)
        args = ["/usr/bin/caffeinate", "-i", *args]
    return {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(REPO),
        # uv marks .venv hidden on this Mac and Python skips hidden .pth files, so point at src directly
        # PYTHONUNBUFFERED: stdout is a file under launchd, so without it run.log fills in 8 KB bursts
        "EnvironmentVariables": {
            "PYTHONPATH": str(REPO / "src"),
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(home() / log),
        "StandardErrorPath": str(home() / log),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _load(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    home().mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(spec))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(path))  # replace a running copy
    r = _launchctl("bootstrap", domain, str(path))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "launchctl bootstrap failed")


def install() -> Path:
    """Install (or reload) both agents: the poller and the web dashboard."""
    _load(PLIST, plist())
    _load(WEB_PLIST, plist(WEB_LABEL, ("web",), "web.log"))
    return PLIST


def uninstall() -> None:
    for path in (PLIST, WEB_PLIST):
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
        path.unlink(missing_ok=True)


def status(label: str = LABEL) -> str:
    r = _launchctl("print", f"gui/{os.getuid()}/{label}")
    if r.returncode != 0:
        return "not installed"
    state = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("state =")), "state = ?")
    pid = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("pid =")), "")
    return f"{state} {pid}".strip()
