import base64
import hashlib
import json
import stat
import threading
import time
import urllib.parse
import urllib.request

import pytest
import respx

from applypilot import gmail

TOKEN = "https://oauth2.googleapis.com/token"


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = gmail._pkce()
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == digest and 43 <= len(verifier) <= 128


def _browser(state_override=None):
    """Stands in for the user: follows the consent URL straight to the loopback redirect."""
    seen = {}

    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen.update(q)
        target = f"{q['redirect_uri']}?code=c0de&state={state_override or q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

    return open_browser, seen


@respx.mock
def test_login_saves_a_private_token_and_sends_the_pkce_verifier(ap_home):
    exchange = respx.post(TOKEN).respond(200, json={
        "access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": gmail.SCOPE})
    respx.get(f"{gmail.API}/profile").respond(200, json={"emailAddress": "me@gmail.com"})
    open_browser, seen = _browser()
    profile = gmail.login(open_browser, timeout_s=10)
    assert profile["emailAddress"] == "me@gmail.com"
    assert seen["scope"] == gmail.SCOPE and seen["access_type"] == "offline" and seen["code_challenge_method"] == "S256"
    sent = dict(urllib.parse.parse_qsl(exchange.calls[0].request.content.decode()))
    assert sent["code"] == "c0de" and sent["redirect_uri"] == seen["redirect_uri"]
    digest = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=")
    assert digest.decode() == seen["code_challenge"]
    path = gmail.token_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "rt"


def test_login_rejects_a_forged_redirect(ap_home):
    open_browser, _ = _browser(state_override="forged")
    with pytest.raises(gmail.GmailAuthError, match="state mismatch"):
        gmail.login(open_browser, timeout_s=10)
    assert not gmail.token_file().exists()


@respx.mock
def test_login_refuses_when_the_read_scope_was_unticked(ap_home):
    respx.post(TOKEN).respond(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                                         "scope": "openid"})
    with pytest.raises(gmail.GmailAuthError, match="not granted"):
        gmail.login(_browser()[0], timeout_s=10)


def _signed_in(ap_home, expires_at):
    gmail._write_secret(gmail.token_file(), {"refresh_token": "rt", "access_token": "old", "expires_at": expires_at,
                                             "obtained_at": time.time(), "email": "me@gmail.com"})


async def test_expired_access_token_is_refreshed_and_saved(ap_home, client):
    _signed_in(ap_home, time.time() - 1)
    with respx.mock:
        refresh = respx.post(TOKEN).respond(200, json={"access_token": "new", "expires_in": 3599})
        msgs = respx.get(f"{gmail.API}/messages").respond(200, json={"messages": [{"id": "a"}, {"id": "b"}]})
        ids = await gmail.Gmail(client).search(gmail.SITES["linkedin"])
    assert ids == ["a", "b"] and refresh.call_count == 1
    assert msgs.calls[0].request.headers["authorization"] == "Bearer new"
    assert json.loads(gmail.token_file().read_text())["access_token"] == "new"


async def test_revoked_sign_in_raises_auth_error(ap_home, client):
    _signed_in(ap_home, time.time() - 1)
    with respx.mock:
        respx.post(TOKEN).respond(400, json={"error": "invalid_grant"})
        with pytest.raises(gmail.GmailAuthError, match="invalid_grant"):
            await gmail.Gmail(client).profile()


async def test_raw_message_is_decoded(ap_home, client):
    _signed_in(ap_home, time.time() + 600)
    eml = b"From: jobalerts-noreply@linkedin.com\r\nSubject: x\r\n\r\nhi"
    raw = base64.urlsafe_b64encode(eml).rstrip(b"=").decode()
    with respx.mock:
        respx.get(f"{gmail.API}/messages/m1").respond(200, json={"raw": raw})
        assert await gmail.Gmail(client).raw("m1") == eml
