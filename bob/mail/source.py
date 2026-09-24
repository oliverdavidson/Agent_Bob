from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class MailMessage:
    id: str
    internet_message_id: str | None
    conversation_id: str | None
    sender_address: str
    sender_name: str | None
    subject: str
    body_text: str
    received_at: datetime
    # Internet message headers in order, e.g. [("Authentication-Results", "spf=pass ...")].
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content_type: str
    data: bytes


class MailSource(Protocol):
    """The mailbox Bob reads. Graph in production; a fake in tests."""

    def list_unprocessed(self, limit: int = 25) -> list[MailMessage]: ...

    def get_attachments(self, message_id: str) -> list[MailAttachment]: ...

    def mark_processed(self, message_id: str) -> None: ...


class MailSender(Protocol):
    """Sends mail as Bob. Graph in production (needs Mail.Send on Bob's mailbox)."""

    def send(self, to: list[str], subject: str, body: str) -> None: ...
