"""Operational tables. QuickBooks stays the ledger of record; these hold workflow state,
evidence and the audit trail."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text
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
    # internal | known | unknown | suspicious (see bob.mail.auth)
    sender_trust: Mapped[str] = mapped_column(String(16))
    # pass | fail | none: SPF/DKIM/DMARC outcome from Authentication-Results
    sender_auth: Mapped[str] = mapped_column(String(8), default="none")
    sender_auth_detail: Mapped[dict] = mapped_column(JSON, default=dict)
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


class QboConnection(Base):
    """The QuickBooks company Bob is connected to, and its OAuth tokens.

    Tokens are stored here for now; move them to Key Vault before production.
    """

    __tablename__ = "qbo_connections"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    realm_id: Mapped[str] = mapped_column(String(32), unique=True)
    environment: Mapped[str] = mapped_column(String(16))
    access_token: Mapped[str] = mapped_column(Text)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_token: Mapped[str] = mapped_column(Text)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class QboAccount(Base):
    """Cached chart of accounts. QBO ids are strings."""

    __tablename__ = "qbo_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    fully_qualified_name: Mapped[str] = mapped_column(String(1024))
    account_type: Mapped[str] = mapped_column(String(64))
    account_sub_type: Mapped[str | None] = mapped_column(String(64))
    acct_num: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QboVendor(Base):
    __tablename__ = "qbo_vendors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(500), index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    active: Mapped[bool] = mapped_column(default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QboTaxCode(Base):
    __tablename__ = "qbo_tax_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Combined purchase-side rate as a fraction, e.g. 0.05 for GST, 0.13 for HST ON.
    purchase_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    active: Mapped[bool] = mapped_column(default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QboSetting(Base):
    """Company-level values from QBO preferences, e.g. closing_date, home_currency."""

    __tablename__ = "qbo_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
