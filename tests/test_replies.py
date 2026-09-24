from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from bob.accounting import posting
from bob.agent.coding import record_proposal
from bob.agent.replies import Changes, ReplyAction, handle_reply, quick_action, strip_quoted
from bob.mail.ingest import poll_mailbox
from bob.mail.outbox import ask_questions, build_digest, digest_due, parse_token, send_digest
from bob.models import Document, InboundEmail, Job, Proposal, Question
from tests.conftest import FakeClaude, FakeMail, make_message
from tests.test_coding import TODAY, make_document, seed_cache
from tests.test_posting import entry

REVIEWER = "oliver@bridgewerk.ca"


class Outbox:
    def __init__(self):
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


@pytest.fixture
def cfg(settings):
    return settings.model_copy(update={"reviewer_addresses": [REVIEWER]})


@pytest.fixture
def held(factory, storage, cfg):
    """A proposal held for approval (acquisition tag), with its question already sent."""
    with factory() as s:
        seed_cache(s)
        doc_id = make_document(s, storage)
        p = record_proposal(
            s,
            s.get(Document, doc_id),
            entry(tags=frozenset({"acquisition_related"})),
            cfg,
            rationale="Falcon matter",
            today=TODAY,
        )
        s.commit()
        pid = p.id
    outbox = Outbox()
    with factory() as s:
        assert ask_questions(s, outbox, cfg) == 1
        s.commit()
    return pid, outbox


def reply(factory, storage, cfg, subject, body, sender=REVIEWER, auth=None):
    headers = [("Authentication-Results", auth)] if auth else []
    msg = make_message(
        hash(body) % 100000, sender=sender, subject=subject, body_text=body, headers=headers
    )
    poll_mailbox(factory, FakeMail(messages=[msg]), storage, cfg)
    with factory() as s:
        return s.scalar(select(InboundEmail.id).where(InboundEmail.graph_message_id == msg.id))


def run_reply(factory, cfg, email_id, claude=None, outbox=None):
    outbox = outbox or Outbox()
    with factory() as s:
        outcome = handle_reply(s, email_id, claude or FakeClaude(None), outbox, cfg)
        s.commit()
    return outcome, outbox


def test_question_email_has_token_details_and_goes_to_reviewer(held, factory):
    pid, outbox = held
    [(to, subject, body)] = outbox.sent
    assert to == [REVIEWER]
    assert subject.startswith(f"[Bob P{pid}] Northwind Cloud Software Inc. CAD 1,260.00")
    assert "Tagged acquisition related" in body and "Reply to this email" in body
    with factory() as s:
        assert s.scalars(select(Question)).one().token == f"P{pid}"


def test_questions_are_not_resent(held, factory, cfg):
    _, outbox = held
    with factory() as s:
        assert ask_questions(s, outbox, cfg) == 0


def test_reviewer_reply_is_routed_to_reply_handler_not_triage(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(factory, storage, cfg, f"RE: [Bob P{pid}] Northwind", "approve")
    with factory() as s:
        kinds = {j.kind for j in s.scalars(select(Job)) if j.payload.get("email_id") == email_id}
        assert kinds == {"handle_reply"}


@pytest.mark.parametrize(
    ("sender", "auth"),
    [
        ("someone@bridgewerk.ca", None),  # internal but not a reviewer
        ("oliver@bridgevverk.ca", None),  # lookalike
        (REVIEWER, "spf=fail; dkim=none; dmarc=fail"),  # spoofed reviewer
    ],
)
def test_other_replies_go_to_triage(held, factory, storage, cfg, sender, auth):
    pid, _ = held
    email_id = reply(
        factory, storage, cfg, f"RE: [Bob P{pid}]", "approve", sender=sender, auth=auth
    )
    with factory() as s:
        kinds = {j.kind for j in s.scalars(select(Job)) if j.payload.get("email_id") == email_id}
        assert kinds == {"triage_email"}


def test_approve_by_reply(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(
        factory,
        storage,
        cfg,
        f"RE: [Bob P{pid}] Northwind",
        "Approve\n\nOn Tue, Bob wrote:\n> approve?",
    )
    outcome, outbox = run_reply(factory, cfg, email_id)
    assert outcome.startswith(f"Approved P{pid}")
    assert outbox.sent[0][0] == [REVIEWER]
    with factory() as s:
        p = s.get(Proposal, pid)
        assert (p.status, p.approved_by) == ("approved", REVIEWER)
        assert s.scalars(select(Question)).one().status in ("answered", "closed")
        assert s.scalars(select(Job).where(Job.kind == "post_proposal")).first() is not None


def test_reject_by_reply(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(factory, storage, cfg, f"RE: [Bob P{pid}]", "Reject: this is Aurora's invoice")
    outcome, _ = run_reply(factory, cfg, email_id)
    assert "Rejected" in outcome
    with factory() as s:
        assert s.get(Proposal, pid).status == "rejected"


def test_change_by_reply_creates_new_approved_version(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(factory, storage, cfg, f"RE: [Bob P{pid}]", "Code it to deal costs please")
    claude = FakeClaude(
        ReplyAction(action="change", reason=None, changes=Changes(account_id="65"), note=None)
    )
    outcome, _ = run_reply(factory, cfg, email_id, claude)
    assert "Changed and approved" in outcome
    with factory() as s:
        old = s.get(Proposal, pid)
        new = s.scalars(select(Proposal).where(Proposal.id != pid)).one()
        assert old.status == "superseded"
        assert (new.status, new.approved_by, new.version) == ("approved", REVIEWER, 2)
        assert new.entry["lines"][0]["account_id"] == "65"


def test_change_to_unknown_account_changes_nothing(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(factory, storage, cfg, f"RE: [Bob P{pid}]", "use account 999")
    claude = FakeClaude(
        ReplyAction(action="change", reason=None, changes=Changes(account_id="999"), note=None)
    )
    outcome, _ = run_reply(factory, cfg, email_id, claude)
    assert "couldn't apply" in outcome
    with factory() as s:
        assert s.get(Proposal, pid).status == "held"


def test_answer_by_reply_recodes_with_note(held, factory, storage, cfg):
    pid, _ = held
    email_id = reply(
        factory, storage, cfg, f"RE: [Bob P{pid}]", "It's the Falcon deal, but the fund pays it"
    )
    claude = FakeClaude(
        ReplyAction(action="answer", reason=None, changes=None, note="The fund pays it")
    )
    run_reply(factory, cfg, email_id, claude)
    with factory() as s:
        p = s.get(Proposal, pid)
        doc = s.get(Document, p.document_id)
        assert p.status == "superseded"
        assert doc.status == "ready_to_code"
        assert "The fund pays it" in doc.summary
        assert any(j.kind == "code_document" for j in s.scalars(select(Job)))


def test_reply_to_already_resolved_item(held, factory, storage, cfg):
    pid, _ = held
    with factory() as s:
        posting.approve(s, pid, "cli:oliver")
        s.commit()
        assert s.scalars(select(Question)).one().status == "closed"
    email_id = reply(factory, storage, cfg, f"RE: [Bob P{pid}]", "reject: no")
    outcome, _ = run_reply(factory, cfg, email_id)
    assert "already approved" in outcome


def test_document_question_approve_sends_it_to_coding(factory, storage, cfg):
    with factory() as s:
        seed_cache(s)
        doc_id = make_document(s, storage, status="needs_human")
        s.get(Document, doc_id).question = "Unknown sender. Is it genuine?"
        s.commit()
    outbox = Outbox()
    with factory() as s:
        ask_questions(s, outbox, cfg)
        s.commit()
    assert outbox.sent[0][1].startswith(f"[Bob D{doc_id}]")
    email_id = reply(factory, storage, cfg, f"RE: [Bob D{doc_id}] Northwind", "yes")
    run_reply(factory, cfg, email_id)
    with factory() as s:
        assert s.get(Document, doc_id).status == "ready_to_code"


def test_quick_actions_and_quote_stripping():
    assert quick_action("Approve").action == "approve"
    assert quick_action("ok").action == "approve"
    assert quick_action("reject - wrong company").reason == "wrong company"
    assert quick_action("no, code it to legal") is None  # not a rejection
    assert quick_action("approve but change the account") is None
    assert (
        strip_quoted("Looks right\n\nOn Mon, Bob <bob@bridgewerk.ca> wrote:\n> question")
        == "Looks right"
    )
    assert parse_token("RE: FW: [Bob P42] Northwind") == ("P", 42)


def test_digest(factory, storage, cfg):
    now = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    with factory() as s:
        seed_cache(s)
        doc_id = make_document(s, storage)
        p = record_proposal(
            s, s.get(Document, doc_id), entry(vendor_id=None), cfg, rationale="x", today=TODAY
        )
        p.status, p.qbo_entity, p.qbo_id, p.posted_at = (
            "posted",
            "Bill",
            "701",
            now - timedelta(hours=3),
        )
        s.commit()
        assert digest_due(s, cfg, now)
        subject, body = build_digest(s, now - timedelta(days=1), now)
        assert "posted 1 ($1,260.00)" in subject
        assert "Posted with flags" in body and "new vendor" in body
        outbox = Outbox()
        assert send_digest(s, outbox, cfg, now)
        s.commit()
        assert not digest_due(s, cfg, now + timedelta(hours=1))  # once a day
        assert not digest_due(s, cfg, datetime(2026, 10, 17, 9, 0, tzinfo=UTC))  # before the hour
        assert digest_due(s, cfg, datetime(2026, 10, 17, 13, 0, tzinfo=UTC))
    assert outbox.sent[0][0] == [REVIEWER]


def test_no_reviewers_means_no_mail(factory, storage, settings):
    with factory() as s:
        assert ask_questions(s, Outbox(), settings) == 0
        assert not send_digest(s, Outbox(), settings)
