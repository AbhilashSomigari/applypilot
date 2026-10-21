import json
from pathlib import Path

import httpx
import pytest

from applypilot import db
from applypilot.config import Settings

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def settings():
    return Settings(ntfy_topic="", mac_notifications=False)


@pytest.fixture
def ap_home(tmp_path, monkeypatch):
    """An APPLYPILOT_HOME with a Desktop OAuth client file (no token yet)."""
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))
    (tmp_path / "gmail_client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret",
        "token_uri": "https://oauth2.googleapis.com/token"}}))
    return tmp_path


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c
