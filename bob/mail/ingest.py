"""Step 1: pull new mail, keep the originals, and queue triage.

Order matters for safety: the email is committed before it is moved out of the inbox, so a
crash can only cause a re-read, which the unique message ids turn into a no-op.
"""

import hashlib
import logging
import mimetypes

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from bob import audit, jobs
from bob.config import Settings
from bob.mail.source import MailMessage, MailSource
from bob.models import Attachment, InboundEmail
from bob.storage import Storage

log = logging.getLogger(__name__)


def sender_trust(address: str, settings: Settings) -> str:
    address = address.lower()
    domain = address.rsplit("@", 1)[-1]
    if domain in {d.lower() for d in settings.internal_domains}:
        return "internal"
    if address in {a.lower() for a in settings.known_sender_addresses}:
        return "known"
    return "unknown"


def _already_ingested(session: Session, msg: MailMessage) -> bool:
    conditions = [InboundEmail.graph_message_id == msg.id]
    if msg.internet_message_id:
        conditions.append(InboundEmail.internet_message_id == msg.internet_message_id)
    return session.scalar(select(InboundEmail.id).where(or_(*conditions)).limit(1)) is not None


def _blob_path(sha256: str, filename: str, content_type: str) -> str:
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()[:10]
    else:
        ext = mimetypes.guess_extension(content_type) or ""
    return f"attachments/{sha256[:2]}/{sha256}{ext}"


def ingest_message(
    session: Session, msg: MailMessage, mail: MailSource, storage: Storage, settings: Settings
) -> InboundEmail | None:
    if _already_ingested(session, msg):
        return None

    email = InboundEmail(
        graph_message_id=msg.id,
        internet_message_id=msg.internet_message_id,
        conversation_id=msg.conversation_id,
        sender_address=msg.sender_address,
        sender_name=msg.sender_name,
        sender_trust=sender_trust(msg.sender_address, settings),
        subject=msg.subject,
        body_text=msg.body_text,
        received_at=msg.received_at,
    )
    session.add(email)
    session.flush()

    for att in mail.get_attachments(msg.id):
        digest = hashlib.sha256(att.data).hexdigest()
        path = _blob_path(digest, att.filename, att.content_type)
        storage.put(path, att.data, att.content_type)
        original = session.scalar(
            select(Attachment)
            .where(Attachment.sha256 == digest, Attachment.duplicate_of_id.is_(None))
            .order_by(Attachment.id)
            .limit(1)
        )
        email.attachments.append(
            Attachment(
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=len(att.data),
                sha256=digest,
                blob_path=path,
                duplicate_of_id=original.id if original else None,
            )
        )
    session.flush()

    audit.record(
        session,
        "email.received",
        "email",
        email.id,
        {
            "from": email.sender_address,
            "trust": email.sender_trust,
            "subject": email.subject,
            "attachments": len(email.attachments),
        },
    )
    jobs.enqueue(session, "triage_email", {"email_id": email.id}, dedupe_key=f"triage:{email.id}")
    return email


def poll_mailbox(
    factory: sessionmaker[Session], mail: MailSource, storage: Storage, settings: Settings
) -> int:
    """Ingest every waiting message, one transaction per message. Returns the number ingested."""
    count = 0
    for msg in mail.list_unprocessed():
        with factory() as session:
            try:
                email = ingest_message(session, msg, mail, storage, settings)
                session.commit()
            except Exception:
                session.rollback()
                log.exception("Failed to ingest message %s; leaving it in the inbox", msg.id)
                continue
        mail.mark_processed(msg.id)
        if email is not None:
            count += 1
    return count
