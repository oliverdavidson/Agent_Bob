from itertools import count

from sqlalchemy import select

from bob.agent.triage import TriagedItem, TriageResult, triage_email
from bob.mail.ingest import poll_mailbox
from bob.mail.source import MailAttachment
from bob.models import AuditEvent, Document, InboundEmail
from tests.conftest import PDF_BYTES, FakeClaude, FakeMail, make_message

_message_numbers = count(1)


def item(index, doc_type, needs_human=False, question=None, counterparty="Vendor Co"):
    return TriagedItem(
        attachment_index=index,
        doc_type=doc_type,
        counterparty=counterparty,
        summary=f"{doc_type} from {counterparty}",
        needs_human=needs_human,
        question=question,
    )


def ingest(factory, storage, settings, sender="billing@vendor.com", files=None):
    files = files if files is not None else [("inv.pdf", "application/pdf", PDF_BYTES)]
    msg = make_message(next(_message_numbers), sender=sender)
    mail = FakeMail(messages=[msg], attachments={msg.id: [MailAttachment(*f) for f in files]})
    poll_mailbox(factory, mail, storage, settings)
    with factory() as s:
        return s.scalar(select(InboundEmail.id).where(InboundEmail.graph_message_id == msg.id))


def run(factory, storage, settings, email_id, claude):
    with factory() as s:
        triage_email(s, email_id, claude, storage, settings)
        s.commit()
    with factory() as s:
        email = s.get(InboundEmail, email_id)
        return email.status, [(d.doc_type, d.status) for d in email.documents]


def test_routes_each_type(factory, storage, settings):
    email_id = ingest(
        factory,
        storage,
        settings,
        files=[
            ("inv.pdf", "application/pdf", b"%PDF invoice"),
            ("stmt.pdf", "application/pdf", b"%PDF statement"),
            ("gl.csv", "text/csv", b"account,debit,credit\n1000,5,0\n"),
        ],
    )
    claude = FakeClaude(
        TriageResult(
            items=[item(0, "vendor_invoice"), item(1, "vendor_statement"), item(2, "gl_export")]
        )
    )

    status, docs = run(factory, storage, settings, email_id, claude)

    assert status == "triaged"
    assert docs == [
        ("vendor_invoice", "ready_to_code"),
        ("vendor_statement", "evidence"),
        ("gl_export", "migration"),
    ]
    content = claude.calls[0]["messages"][0]["content"]
    assert any(b["type"] == "document" and b["title"] == "inv.pdf" for b in content)
    assert any("account,debit,credit" in b.get("text", "") for b in content)
    assert claude.calls[0]["output_config"] == {"effort": "medium"}
    with factory() as s:
        event = s.scalars(select(AuditEvent).where(AuditEvent.action == "email.triaged")).one()
        assert event.data["prompt_version"] == "triage_v1"


def test_duplicate_file_skips_the_model(factory, storage, settings):
    ingest(factory, storage, settings)
    email_id = ingest(factory, storage, settings)  # same PDF bytes again
    claude = FakeClaude(TriageResult(items=[]))

    _status, docs = run(factory, storage, settings, email_id, claude)

    assert docs == [("duplicate", "duplicate")]
    content = claude.calls[0]["messages"][0]["content"]
    assert not any(b["type"] == "document" for b in content)


def test_unknown_sender_cannot_go_straight_to_coding(factory, storage, settings):
    email_id = ingest(factory, storage, settings, sender="ap@lookalike-vendor.co")
    claude = FakeClaude(TriageResult(items=[item(0, "vendor_invoice")]))

    _, docs = run(factory, storage, settings, email_id, claude)

    assert docs == [("vendor_invoice", "needs_human")]
    with factory() as s:
        assert "unknown sender" in s.scalars(select(Document)).one().question


def test_attachment_the_model_skipped_is_still_recorded(factory, storage, settings):
    email_id = ingest(
        factory,
        storage,
        settings,
        files=[("a.pdf", "application/pdf", b"%PDF a"), ("b.pdf", "application/pdf", b"%PDF b")],
    )
    claude = FakeClaude(TriageResult(items=[item(0, "receipt"), item(7, "receipt")]))

    _, docs = run(factory, storage, settings, email_id, claude)

    assert docs == [("receipt", "ready_to_code"), ("other", "needs_human")]


def test_refusal_holds_the_email(factory, storage, settings):
    email_id = ingest(factory, storage, settings)
    claude = FakeClaude(None, stop_reason="refusal")

    status, docs = run(factory, storage, settings, email_id, claude)

    assert (status, docs) == ("held", [])


def test_retried_job_does_not_triage_twice(factory, storage, settings):
    email_id = ingest(factory, storage, settings)
    claude = FakeClaude(TriageResult(items=[item(0, "vendor_invoice")]))
    run(factory, storage, settings, email_id, claude)
    run(factory, storage, settings, email_id, claude)
    assert len(claude.calls) == 1
