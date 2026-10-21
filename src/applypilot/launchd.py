"""Install the poller as a per-user launchd agent that starts at login and restarts if it dies."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from applypilot.config import home

LABEL = "com.applypilot.poller"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
REPO = Path(__file__).resolve().parents[2]


def plist() -> dict:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    return {
        "Label": LABEL,
        # caffeinate -i keeps the Mac from idle-sleeping while the poller runs (lid-closed sleep still wins)
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", uv, "run", "--project", str(REPO), "applypilot", "run"],
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
        "StandardOutPath": str(home() / "run.log"),
        "StandardErrorPath": str(home() / "run.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def install() -> Path:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    home().mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist()))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(PLIST))  # replace a running copy
    r = _launchctl("bootstrap", domain, str(PLIST))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "launchctl bootstrap failed")
    return PLIST


def uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}", str(PLIST))
    PLIST.unlink(missing_ok=True)


def status() -> str:
    r = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if r.returncode != 0:
        return "not installed"
    state = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("state =")), "state = ?")
    pid = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("pid =")), "")
    return f"{state} {pid}".strip()
