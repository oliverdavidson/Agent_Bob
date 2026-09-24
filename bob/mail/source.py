from dataclasses import dataclass
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
