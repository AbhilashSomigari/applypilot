"""Read-only Gmail access, for job-alert emails (LinkedIn, Indeed, Handshake, Glassdoor, ZipRecruiter, Dice).

OAuth for an installed app: a Desktop client from Google Cloud (`~/.applypilot/gmail_client_secret.json`), a
loopback redirect with PKCE, and a refresh token saved to `~/.applypilot/gmail_token.json` (mode 600). The only
scope is `gmail.readonly`. While the Google Cloud app is in "Testing", Google revokes the refresh token 7 days
after sign-in; a revoked token raises `GmailAuthError` and `gmail login` signs in again.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from applypilot.config import home

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TESTING_TOKEN_DAYS = 7

# Gmail search per alert source; the ingester reads only what these match
SITES = {
    "linkedin": "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)",
    # jobalert = saved-search alerts, match = "jobs you may like"; indeedapply@ (application receipts) is not a lead
    "indeed": "from:(donotreply@jobalert.indeed.com OR donotreply@match.indeed.com)",
    "handshake": "from:m.joinhandshake.com",
    "glassdoor": "from:glassdoor.com",
    "ziprecruiter": "from:ziprecruiter.com",
    "dice": "from:dice.com",
}


class GmailAuthError(Exception):
    """Not signed in, or Google revoked the sign-in (Testing-mode apps: after 7 days)."""


def client_file() -> Path:
    return home() / "gmail_client_secret.json"


def token_file() -> Path:
    return home() / "gmail_token.json"


def _client() -> dict:
    path = client_file()
    if not path.exists():
        raise GmailAuthError(f"no OAuth client at {path}: download the Desktop client JSON from Google Cloud")
    data = json.loads(path.read_text())
    if "installed" not in data:
        raise GmailAuthError("the client JSON is not a Desktop app client (create one of type 'Desktop app')")
    return data["installed"]


def _write_secret(path: Path, data: dict) -> None:
    # created 600 from the start, so the token is never briefly world-readable
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_code(server: http.server.HTTPServer, state: str, timeout_s: float) -> str:
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's naming)
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            got.update(q)
            ok = "code" in q and q.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if ok else "Sign-in failed. Check the terminal."
            self.wfile.write(f"<p style='font:16px system-ui'>ApplyPilot: {msg}</p>".encode())

        def log_message(self, *_):
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1
    deadline = time.time() + timeout_s
    while not got and time.time() < deadline:
        server.handle_request()
    if not got:
        raise GmailAuthError("timed out waiting for the browser sign-in")
    if "error" in got:
        raise GmailAuthError(f"Google returned an error: {got['error']}")
    if got.get("state") != state:
        raise GmailAuthError("state mismatch on the sign-in redirect (ignored it)")
    return got["code"]


def login(open_browser=webbrowser.open, timeout_s: float = 300) -> dict:
    """Interactive sign-in. Saves the refresh token and returns the Gmail profile (emailAddress, ...)."""
    c = _client()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    with http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler) as server:
        redirect = f"http://127.0.0.1:{server.server_address[1]}/"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": c["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            # consent: always return a refresh token; select_account: don't silently use the wrong Google account
            "prompt": "consent select_account",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        print(f"Opening your browser to sign in. If it doesn't open, visit:\n{url}\n")
        open_browser(url)
        code = _wait_for_code(server, state, timeout_s)
    r = httpx.post(c.get("token_uri") or "https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, timeout=30)
    if r.status_code != 200:
        raise GmailAuthError(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    tok = r.json()
    if "refresh_token" not in tok:
        raise GmailAuthError("Google returned no refresh token; remove ApplyPilot's access in your Google "
                             "account (Security → Third-party connections) and sign in again")
    if SCOPE not in tok.get("scope", "").split():
        raise GmailAuthError("the Gmail read permission was not granted (tick it on the consent screen)")
    profile = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {tok['access_token']}"},
                        timeout=30).json()
    _write_secret(token_file(), {
        "refresh_token": tok["refresh_token"],
        "access_token": tok["access_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
        "obtained_at": time.time(),
        "email": profile.get("emailAddress"),
    })
    return profile


def logout() -> bool:
    """Revoke the refresh token at Google and delete it locally."""
    path = token_file()
    if not path.exists():
        return False
    tok = json.loads(path.read_text())
    try:
        httpx.post("https://oauth2.googleapis.com/revoke", data={"token": tok["refresh_token"]}, timeout=30)
    except httpx.HTTPError:
        pass  # deleting the local copy is what matters
    path.unlink()
    return True


def token_info() -> dict | None:
    path = token_file()
    return json.loads(path.read_text()) if path.exists() else None


class Gmail:
    """Async read-only Gmail client sharing the app's httpx client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._tok = token_info()
        if not self._tok:
            raise GmailAuthError("not signed in: run `applypilot gmail login`")

    async def _access_token(self) -> str:
        if self._tok.get("access_token") and self._tok.get("expires_at", 0) > time.time():
            return self._tok["access_token"]
        c = _client()
        r = await self.client.post(c.get("token_uri") or "https://oauth2.googleapis.com/token", data={
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "refresh_token": self._tok["refresh_token"],
            "grant_type": "refresh_token",
        })
        if r.status_code in (400, 401):
            # invalid_grant: revoked, or the 7-day limit of a Testing-mode app
            raise GmailAuthError(f"Gmail sign-in expired ({r.json().get('error', r.status_code)}): "
                                 "run `applypilot gmail login`")
        r.raise_for_status()
        tok = r.json()
        self._tok.update(access_token=tok["access_token"], expires_at=time.time() + int(tok["expires_in"]) - 60)
        _write_secret(token_file(), self._tok)
        return self._tok["access_token"]

    async def _get(self, path: str, **params) -> dict:
        r = await self.client.get(f"{API}/{path}", params=params,
                                  headers={"Authorization": f"Bearer {await self._access_token()}"})
        if r.status_code == 401:
            self._tok["expires_at"] = 0  # access token rejected early: refresh once and retry
            r = await self.client.get(f"{API}/{path}", params=params,
                                      headers={"Authorization": f"Bearer {await self._access_token()}"})
        r.raise_for_status()
        return r.json()

    async def profile(self) -> dict:
        return await self._get("profile")

    async def search(self, q: str, max_results: int = 100) -> list[str]:
        """Message ids matching a Gmail search, newest first."""
        data = await self._get("messages", q=q, maxResults=max_results)
        return [m["id"] for m in data.get("messages", [])]

    async def headers(self, msg_id: str) -> dict:
        data = await self._get(f"messages/{msg_id}", format="metadata", metadataHeaders=["From", "Subject", "Date"])
        h = {x["name"].lower(): x["value"] for x in data.get("payload", {}).get("headers", [])}
        return {"id": msg_id, "internal_date": int(data.get("internalDate", 0)) / 1000,
                "snippet": data.get("snippet", ""), **h}

    async def message(self, msg_id: str) -> tuple[bytes, float]:
        """The full RFC 822 message (what an .eml file holds) and when Gmail received it."""
        data = await self._get(f"messages/{msg_id}", format="raw")
        raw = base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
        return raw, int(data.get("internalDate", 0)) / 1000

    async def raw(self, msg_id: str) -> bytes:
        return (await self.message(msg_id))[0]
