from sqlalchemy import func, select

from bob.mail.ingest import poll_mailbox
from bob.mail.source import MailAttachment
from bob.models import Attachment, AuditEvent, InboundEmail, Job
from tests.conftest import PDF_BYTES, FakeMail, make_message


def test_ingest_stores_email_attachment_and_queues_triage(factory, storage, settings):
    mail = FakeMail(
        messages=[make_message(1)],
        attachments={"graph-1": [MailAttachment("inv-1.pdf", "application/pdf", PDF_BYTES)]},
    )

    assert poll_mailbox(factory, mail, storage, settings) == 1
    assert mail.processed == ["graph-1"]

    with factory() as s:
        email = s.scalars(select(InboundEmail)).one()
        assert email.sender_trust == "known"
        [att] = email.attachments
        assert att.duplicate_of_id is None
        assert storage.get(att.blob_path) == PDF_BYTES
        job = s.scalars(select(Job)).one()
        assert (job.kind, job.payload, job.dedupe_key) == (
            "triage_email",
            {"email_id": email.id},
            f"triage:{email.id}",
        )
        assert s.scalar(select(AuditEvent.action)) == "email.received"


def test_reading_the_same_message_twice_is_a_no_op(factory, storage, settings):
    mail = FakeMail(messages=[make_message(1)])
    poll_mailbox(factory, mail, storage, settings)
    mail.processed.clear()  # simulate a crash before the move

    assert poll_mailbox(factory, mail, storage, settings) == 0
    with factory() as s:
        assert s.scalar(select(func.count(InboundEmail.id))) == 1
        assert s.scalar(select(func.count(Job.id))) == 1


def test_same_file_in_a_later_email_is_marked_duplicate(factory, storage, settings):
    pdf = MailAttachment("inv-1.pdf", "application/pdf", PDF_BYTES)
    mail = FakeMail(
        messages=[make_message(1), make_message(2, subject="Fwd: Invoice 1")],
        attachments={"graph-1": [pdf], "graph-2": [pdf]},
    )
    poll_mailbox(factory, mail, storage, settings)

    with factory() as s:
        first, second = s.scalars(select(Attachment).order_by(Attachment.id)).all()
        assert first.duplicate_of_id is None
        assert second.duplicate_of_id == first.id
        assert first.blob_path == second.blob_path


def test_failed_message_stays_in_inbox(factory, storage, settings):
    class BrokenMail(FakeMail):
        def get_attachments(self, message_id):
            raise RuntimeError("graph timeout")

    mail = BrokenMail(messages=[make_message(1)])
    assert poll_mailbox(factory, mail, storage, settings) == 0
    assert mail.processed == []
    with factory() as s:
        assert s.scalar(select(func.count(InboundEmail.id))) == 0
