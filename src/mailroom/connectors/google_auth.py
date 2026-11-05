"""Google OAuth (installed-app / loopback flow).

One desktop OAuth client covers Gmail, Calendar and Sheets. The client JSON and
the resulting refresh-token bundle both live in the macOS Keychain, never on
disk. Scopes are deliberately minimal:

  gmail.modify   - read messages, create drafts, manage labels. NOT gmail.send;
                   the app must be incapable of sending mail on its own.
  calendar.readonly + calendar.events - read freebusy, and (later) create events
                   only after you approve them.
  spreadsheets   - append the recap rows.
  documents + drive.file - create the Email Assistant recap Google Doc. drive.file
                   only reaches files and folders this app created.
"""
from __future__ import annotations

import json
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ..secrets_store import store
from ..settings import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


def _load_credentials() -> Credentials | None:
    blob = store.get("google_token_json")
    if not blob:
        return None
    # Use the scopes actually granted (stored in the token), not the requested list:
    # forcing new scopes onto an old token makes the refresh fail with invalid_scope.
    return Credentials.from_authorized_user_info(json.loads(blob))


def _save_credentials(creds: Credentials) -> None:
    store.set("google_token_json", creds.to_json())


def authorize() -> dict[str, Any]:
    """Run the consent flow in the default browser. Blocking; call from a worker."""
    client_json = store.require("google_client_secret_json")
    flow = InstalledAppFlow.from_client_config(json.loads(client_json), SCOPES)
    creds = flow.run_local_server(port=settings.oauth_port, open_browser=True,
                                  prompt="consent", access_type="offline")
    _save_credentials(creds)
    return {"authorized": True, "scopes": list(creds.scopes or [])}


def credentials() -> Credentials:
    creds = _load_credentials()
    if not creds:
        raise RuntimeError("Google account not connected. Run the OAuth flow in Settings.")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_credentials(creds)
        else:
            raise RuntimeError("Google credentials expired and cannot be refreshed; re-authorize.")
    return creds


def status() -> dict[str, Any]:
    creds = _load_credentials()
    if not creds:
        return {"connected": False, "reason": "no token stored"}
    return {
        "connected": True,
        "valid": bool(creds.valid),
        "expired": bool(creds.expired),
        "has_refresh_token": bool(creds.refresh_token),
        "scopes": list(creds.scopes or []),
        "missing_scopes": [s for s in SCOPES if s not in (creds.scopes or [])],
    }


def service(api: str, version: str):
    return build(api, version, credentials=credentials(), cache_discovery=False)
