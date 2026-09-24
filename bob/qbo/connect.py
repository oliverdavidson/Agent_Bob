"""Connect Bob to a QuickBooks company (run once, and again if the connection lapses).

    python -m bob.qbo.connect

Prints an Intuit sign-in link. After you approve access, the browser is sent to the redirect
URI; paste that full URL back here. Nothing needs to be listening on the redirect URI, so
Bob's Container App can stay without public ingress.
"""

import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from bob import audit
from bob.config import get_settings
from bob.db import make_engine, make_sessionmaker
from bob.models import QboConnection
from bob.qbo import oauth


def parse_redirect(url: str, expected_state: str) -> tuple[str, str]:
    """Return (code, realm_id) from the URL the browser landed on."""
    params = parse_qs(urlparse(url.strip()).query)
    if "error" in params:
        raise ValueError(f"Intuit returned an error: {params['error'][0]}")
    if params.get("state", [""])[0] != expected_state:
        raise ValueError("State does not match; start again.")
    try:
        return params["code"][0], params["realmId"][0]
    except KeyError as missing:
        raise ValueError(f"The URL has no {missing}; paste the whole address.") from None


def main() -> int:
    settings = get_settings()
    if not (settings.qbo_client_id and settings.qbo_client_secret):
        print("Set BOB_QBO_CLIENT_ID and BOB_QBO_CLIENT_SECRET first.")
        return 1

    state = secrets.token_urlsafe(16)
    print(f"QuickBooks environment: {settings.qbo_environment}\n")
    print("1. Open this link and approve access for Bob:\n")
    print(oauth.authorization_url(settings.qbo_client_id, settings.qbo_redirect_uri, state))
    print("\n2. Paste the full URL your browser was sent to (it may show an error page):")
    code, realm_id = parse_redirect(input("> "), state)

    with httpx.Client(timeout=30) as http:
        tokens = oauth.exchange_code(
            http,
            settings.qbo_client_id,
            settings.qbo_client_secret,
            code,
            settings.qbo_redirect_uri,
        )

    factory = make_sessionmaker(make_engine(settings.database_url))
    with factory() as session:
        conn = session.scalars(
            select(QboConnection).where(QboConnection.environment == settings.qbo_environment)
        ).first()
        if conn and conn.realm_id != realm_id:
            print(f"Already connected to company {conn.realm_id}; disconnect it first.")
            return 1
        conn = conn or QboConnection(realm_id=realm_id, environment=settings.qbo_environment)
        conn.access_token = tokens.access_token
        conn.access_expires_at = tokens.access_expires_at
        conn.refresh_token = tokens.refresh_token
        conn.refresh_expires_at = tokens.refresh_expires_at
        session.add(conn)
        session.flush()
        audit.record(
            session,
            "qbo.connected",
            "qbo",
            conn.id,
            {"realm_id": realm_id, "environment": settings.qbo_environment},
            actor="cli",
        )
        session.commit()
    print(f"\nConnected to QuickBooks company {realm_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
