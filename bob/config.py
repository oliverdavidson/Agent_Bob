"""Runtime configuration, read from BOB_* environment variables (or a local .env file).

List values are JSON in the environment, e.g. BOB_INTERNAL_DOMAINS='["bridgewerk.ca"]'.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOB_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://bob:bob@localhost:5432/bob"

    # Mailbox. Graph access must be scoped to this one mailbox with Exchange RBAC for
    # Applications; see README.
    mailbox: str = "bob@bridgewerk.ca"
    internal_domains: list[str] = ["bridgewerk.ca"]
    known_sender_addresses: list[str] = []
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    # Local development only. In Azure the app uses its managed identity.
    graph_client_secret: str | None = None
    mail_poll_seconds: int = 60
    processed_folder: str = "Processed"

    # Document storage. Local filesystem in dev, Blob Storage in Azure.
    blob_account_url: str | None = None
    blob_container: str = "documents"
    local_storage_dir: str = ".data/blobs"

    # Claude on Microsoft Foundry. Uses Entra ID (managed identity) unless an API key is set.
    foundry_resource: str | None = None
    foundry_api_key: str | None = None
    model: str = "claude-opus-5-5"
    triage_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    coding_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # QuickBooks Online. Writes stay off until the posting slice is built and tested.
    qbo_environment: Literal["sandbox", "production"] = "sandbox"
    qbo_client_id: str | None = None
    qbo_client_secret: str | None = None
    # Must match a redirect URI registered on the Intuit app. Nothing needs to be listening
    # there: the connect command asks you to paste the URL the browser lands on.
    qbo_redirect_uri: str = "http://localhost:8765/qbo/callback"
    qbo_minor_version: int = 75
    qbo_writes_enabled: bool = False
    qbo_sync_hours: int = 6

    # Accounting policy. Placeholder until agreed with the accountant.
    materiality: Decimal = Decimal(25000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
