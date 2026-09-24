"""Intuit OAuth 2.0 for QuickBooks Online.

The accounting scope grants read and write access; Intuit has no read-only scope. Read-only
operation is enforced in bob.qbo.client, not here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from bob.db import utcnow

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def _token_request(http: httpx.Client, client_id: str, secret: str, form: dict) -> TokenSet:
    now = utcnow()
    response = http.post(
        TOKEN_URL,
        data=form,
        auth=(client_id, secret),
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise OAuthError(f"Intuit token request failed ({response.status_code}): {response.text}")
    body = response.json()
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        access_expires_at=now + timedelta(seconds=int(body["expires_in"])),
        refresh_expires_at=now + timedelta(seconds=int(body["x_refresh_token_expires_in"])),
    )


def exchange_code(
    http: httpx.Client, client_id: str, secret: str, code: str, redirect_uri: str
) -> TokenSet:
    return _token_request(
        http,
        client_id,
        secret,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )


def refresh(http: httpx.Client, client_id: str, secret: str, refresh_token: str) -> TokenSet:
    """Intuit may return a new refresh token; the caller must store whatever comes back."""
    return _token_request(
        http, client_id, secret, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
