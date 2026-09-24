"""QuickBooks Online API client.

Intuit's OAuth scope always grants write access, so this client is where "read-only" is
enforced: every write goes through `create`, which refuses unless BOB_QBO_WRITES_ENABLED is
true. Reads are GET requests; the only POSTs are writes.
"""

import logging
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bob.config import Settings
from bob.db import as_utc, utcnow
from bob.models import QboConnection
from bob.qbo import oauth

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
PAGE_SIZE = 1000
REFRESH_MARGIN = timedelta(minutes=5)
RETRY_STATUSES = {429, 500, 502, 503, 504}


class WritesDisabled(RuntimeError):
    pass


class NotConnected(RuntimeError):
    pass


class QBOError(RuntimeError):
    def __init__(self, status: int, errors: list[dict]):
        self.status = status
        self.errors = errors
        detail = "; ".join(
            f"{e.get('code', '?')}: {e.get('Message', '')} {e.get('Detail', '')}".strip()
            for e in errors
        )
        super().__init__(f"QuickBooks returned {status}: {detail}")


class QBOClient:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        self.factory = factory
        self.settings = settings
        self.http = http or httpx.Client(timeout=60)
        self.base = BASE_URLS[settings.qbo_environment]
        self._sleep = sleep

    # --- tokens -----------------------------------------------------------------------------

    def _connection_query(self, session: Session):
        stmt = select(QboConnection).where(
            QboConnection.environment == self.settings.qbo_environment
        )
        if session.get_bind().dialect.name == "postgresql":
            # Serialises refreshes: Intuit may rotate the refresh token, and two workers
            # refreshing at once would leave one holding a revoked token.
            stmt = stmt.with_for_update()
        return stmt

    def _access(self, force_refresh: bool = False) -> tuple[str, str]:
        """Return (realm_id, access_token), refreshing if it is about to expire."""
        with self.factory() as session:
            conn = session.scalars(self._connection_query(session)).first()
            if conn is None:
                raise NotConnected("No QuickBooks connection. Run: python -m bob.qbo.connect")
            if force_refresh or as_utc(conn.access_expires_at) <= utcnow() + REFRESH_MARGIN:
                tokens = oauth.refresh(
                    self.http,
                    self.settings.qbo_client_id,
                    self.settings.qbo_client_secret,
                    conn.refresh_token,
                )
                conn.access_token = tokens.access_token
                conn.access_expires_at = tokens.access_expires_at
                conn.refresh_token = tokens.refresh_token
                conn.refresh_expires_at = tokens.refresh_expires_at
            realm, token = conn.realm_id, conn.access_token
            session.commit()
        return realm, token

    # --- requests ---------------------------------------------------------------------------

    def _request(
        self, method: str, path: str, params: dict | None = None, json: Any = None
    ) -> dict:
        realm, token = self._access()
        params = {"minorversion": str(self.settings.qbo_minor_version), **(params or {})}
        refreshed = False
        for attempt in range(4):
            response = self.http.request(
                method,
                f"{self.base}/v3/company/{realm}/{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code == 401 and not refreshed:
                realm, token = self._access(force_refresh=True)
                refreshed = True
                continue
            if response.status_code in RETRY_STATUSES and attempt < 3:
                self._sleep(2**attempt)
                continue
            break
        body = response.json() if response.content else {}
        if response.status_code >= 400 or "Fault" in body:
            errors = (body.get("Fault") or {}).get("Error") or [{"Message": response.text[:500]}]
            raise QBOError(response.status_code, errors)
        return body

    def query(self, statement: str) -> list[dict]:
        """Run a QBO query, following pages. `statement` must not include STARTPOSITION."""
        entity = statement.split()[3] if statement.lower().startswith("select * from") else None
        rows: list[dict] = []
        start = 1
        while True:
            body = self._request(
                "GET",
                "query",
                {"query": f"{statement} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"},
            )
            result = body.get("QueryResponse", {})
            page = (
                result.get(entity, [])
                if entity
                else next((v for v in result.values() if isinstance(v, list)), [])
            )
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            start += PAGE_SIZE

    def read(self, entity: str, entity_id: str) -> dict:
        return self._request("GET", f"{entity.lower()}/{entity_id}")[entity]

    def preferences(self) -> dict:
        return self._request("GET", "preferences")["Preferences"]

    def create(self, entity: str, payload: dict, request_id: str) -> dict:
        """Create an object. `request_id` makes retries idempotent on Intuit's side."""
        if not self.settings.qbo_writes_enabled:
            raise WritesDisabled(
                f"Refusing to create {entity}: QuickBooks writes are disabled "
                "(BOB_QBO_WRITES_ENABLED=false)."
            )
        body = self._request("POST", entity.lower(), {"requestid": request_id}, json=payload)
        return body[entity]
