"""Claude Code OAuth credential manager for the Anthropic SDK.

Reads the access token from ~/.claude/.credentials.json (written by the
Claude Code CLI) and refreshes it automatically when it expires.
Refresh tokens rotate on every use — both tokens are saved back to the
credentials file so the next call picks them up.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_REFRESH_URL = "https://console.anthropic.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_lock = threading.Lock()


def _read_creds() -> dict:
    return json.loads(_CREDS_PATH.read_text(encoding="utf-8"))


def _write_creds(creds: dict) -> None:
    _CREDS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")


def get_access_token() -> str:
    with _lock:
        return _read_creds()["claudeAiOauth"]["accessToken"]


def refresh_access_token() -> str:
    with _lock:
        creds = _read_creds()
        oauth = creds["claudeAiOauth"]
        log.info("Refreshing Claude Code OAuth access token...")
        resp = httpx.post(
            _REFRESH_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": _CLIENT_ID,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        oauth["accessToken"] = data["access_token"]
        oauth["refreshToken"] = data["refresh_token"]
        creds["claudeAiOauth"] = oauth
        _write_creds(creds)
        log.info("Claude Code OAuth token refreshed successfully.")
        return data["access_token"]
