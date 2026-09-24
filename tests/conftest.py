import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bob.config import Settings
from bob.db import Base, make_engine, make_sessionmaker
from bob.mail.source import MailAttachment, MailMessage
from bob.storage import LocalStorage


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        # Set BOB_TEST_DATABASE_URL to run against Postgres (exercises SKIP LOCKED).
        database_url=os.environ.get("BOB_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'bob.db'}"),
        local_storage_dir=str(tmp_path / "blobs"),
        known_sender_addresses=["billing@vendor.com"],
        mail_poll_seconds=0,
    )


@pytest.fixture
def factory(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def storage(settings) -> LocalStorage:
    return LocalStorage(settings.local_storage_dir)


def make_message(n: int, sender: str = "billing@vendor.com", **kw) -> MailMessage:
    defaults = {
        "id": f"graph-{n}",
        "internet_message_id": f"<msg-{n}@vendor.com>",
        "conversation_id": f"conv-{n}",
        "sender_address": sender,
        "sender_name": "Vendor Billing",
        "subject": f"Invoice {n}",
        "body_text": "Please find our invoice attached.",
        "received_at": datetime(2026, 10, 3, 15, 0, tzinfo=UTC),
    }
    defaults.update(kw)
    return MailMessage(**defaults)


@dataclass
class FakeMail:
    messages: list[MailMessage] = field(default_factory=list)
    attachments: dict[str, list[MailAttachment]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)

    def list_unprocessed(self, limit: int = 25) -> list[MailMessage]:
        return [m for m in self.messages if m.id not in self.processed][:limit]

    def get_attachments(self, message_id: str) -> list[MailAttachment]:
        return self.attachments.get(message_id, [])

    def mark_processed(self, message_id: str) -> None:
        self.processed.append(message_id)


class FakeClaude:
    """Stands in for AnthropicFoundry; returns a canned parse() response."""

    def __init__(self, parsed=None, stop_reason: str = "end_turn"):
        self.calls: list[dict] = []
        self.parsed = parsed
        self.stop_reason = stop_reason
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.parsed, stop_reason=self.stop_reason, stop_details=None
        )


PDF_BYTES = b"%PDF-1.4 fake invoice"
