"""Operational tables. QuickBooks stays the ledger of record; these hold workflow state,
evidence and the audit trail."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bob.db import Base, utcnow

# Postgres BIGSERIAL, SQLite INTEGER PRIMARY KEY (so tests can run on SQLite).
PK = BigInteger().with_variant(Integer(), "sqlite")


class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    graph_message_id: Mapped[str] = mapped_column(String(512), unique=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(512))
    sender_address: Mapped[str] = mapped_column(String(320), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(320))
    # internal | known | unknown
    sender_trust: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # received | triaged | held | failed
    status: Mapped[str] = mapped_column(String(16), default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="email", order_by="Attachment.id"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="email", order_by="Document.id"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("inbound_emails.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    blob_path: Mapped[str] = mapped_column(String(1024))
    # Set when the same bytes arrived earlier; the earlier attachment is the original.
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    email: Mapped[InboundEmail] = relationship(back_populates="attachments")


class Document(Base):
    """One accounting-relevant item found in an email: an attachment or the email body."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("inbound_emails.id"), index=True)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"))
    doc_type: Mapped[str] = mapped_column(String(32), index=True)
    # triaged | needs_human | duplicate | not_posted | ... (later slices add coding/posting)
    status: Mapped[str] = mapped_column(String(32), index=True)
    counterparty: Mapped[str | None] = mapped_column(String(320))
    summary: Mapped[str] = mapped_column(Text, default="")
    question: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    email: Mapped[InboundEmail] = relationship(back_populates="documents")


class Job(Base):
    """Postgres-backed work queue. Claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Prevents the same unit of work being queued twice (e.g. "triage:42").
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditEvent(Base):
    """Append-only. The Postgres migration installs a trigger that rejects UPDATE and DELETE."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int | None] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
