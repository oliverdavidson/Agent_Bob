"""Act on a reviewer's reply to one of Bob's questions.

Only replies from BOB_REVIEWER_ADDRESSES, from our own domain, that did not fail sender
authentication, and whose subject carries a [Bob P42]/[Bob D17] token, reach this module
(see bob.mail.ingest). Anything else goes through normal triage.

Plain "approve" / "reject: reason" are recognised without the model. Anything else is read by
Claude into one structured action, which code then applies with the usual checks: a change
creates a new proposal version, still validated, approved by the reviewer.
"""

import logging
import re
from dataclasses import replace
from typing import Literal

from anthropic import AnthropicFoundry
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit, jobs
from bob.accounting import posting
from bob.accounting.entry import ALL_TAGS, entry_from_dict
from bob.agent.coding import record_proposal, reference_data
from bob.config import Settings
from bob.db import utcnow
from bob.mail.outbox import parse_token
from bob.mail.source import MailSender
from bob.models import Document, InboundEmail, Proposal, QboAccount, QboTaxCode, QboVendor, Question

log = logging.getLogger(__name__)

PROMPT_VERSION = "reply_v1"
APPROVE = re.compile(
    r"^\s*(approve[d]?|yes|ok(ay)?|post it|go ahead|looks good)\b[.!]?\s*$", re.IGNORECASE
)
# Deliberately narrow: "no, code it to Legal" must not be read as a rejection.
REJECT = re.compile(
    r"^\s*(reject(ed)?|don'?t book( it)?)\b[\s:,-]*(?P<reason>.*)$", re.IGNORECASE | re.DOTALL
)
QUOTE_START = re.compile(
    r"^(On .+wrote:|From: .+|-----Original Message-----|_{5,})\s*$", re.MULTILINE
)

SYSTEM_PROMPT = """You are Bob, BridgeWerk's bookkeeping agent. A reviewer has replied to a question you asked about a proposed accounting entry or an incoming document. Turn the reply into exactly one action:

- approve: book it as proposed (or, for a document, go ahead and code it).
- reject: do not book it. Put their reason in `reason`.
- change: book it with specific changes. Fill only the fields they asked to change, using ids from the QuickBooks lists provided. account_id and tax_code_id apply to every line.
- answer: they gave information rather than a decision (for example "this is the Falcon deal" or "it's for Aurora, not us"). Put the information in `note`; Bob will re-code the item with it.
- unclear: you cannot tell what they want. Put the clarifying question in `note`.

Never invent ids. If the reviewer names an account or vendor you cannot find in the lists, use "answer" with their words in `note`."""


class Changes(BaseModel):
    account_id: str | None = None
    tax_code_id: str | None = None
    vendor_id: str | None = None
    kind: Literal["bill", "expense", "credit"] | None = None
    invoice_date: str | None = None
    tags_add: list[str] = []
    tags_remove: list[str] = []


class ReplyAction(BaseModel):
    action: Literal["approve", "reject", "change", "answer", "unclear"]
    reason: str | None
    changes: Changes | None
    note: str | None


def strip_quoted(body: str) -> str:
    """The reviewer's own words: drop quoted history and '>' lines."""
    match = QUOTE_START.search(body)
    text = body[: match.start()] if match else body
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    ).strip()


def quick_action(text: str) -> ReplyAction | None:
    if APPROVE.match(text):
        return ReplyAction(action="approve", reason=None, changes=None, note=None)
    if m := REJECT.match(text):
        return ReplyAction(
            action="reject", reason=m.group("reason").strip() or None, changes=None, note=None
        )
    return None


def interpret(
    client: AnthropicFoundry, settings: Settings, session: Session, question: Question, text: str
) -> ReplyAction:
    context = question.body
    response = client.messages.parse(
        model=settings.model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": reference_data(session)},
                    {"type": "text", "text": f"<your_question>\n{context}\n</your_question>"},
                    {"type": "text", "text": f"<reviewer_reply>\n{text}\n</reviewer_reply>"},
                ],
            }
        ],
        thinking={"type": "adaptive"},
        output_config={"effort": settings.reply_effort},
        output_format=ReplyAction,
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        return ReplyAction(
            action="unclear", reason=None, changes=None, note="Could you say that another way?"
        )
    return response.parsed_output


def _apply_changes(session: Session, proposal: Proposal, changes: Changes):
    """Return a modified entry, or raise ValueError naming what could not be applied."""
    entry = entry_from_dict(proposal.entry)
    if changes.account_id:
        if session.get(QboAccount, changes.account_id) is None:
            raise ValueError(f"account {changes.account_id} does not exist")
        entry = replace(
            entry, lines=tuple(replace(line, account_id=changes.account_id) for line in entry.lines)
        )
    if changes.tax_code_id:
        if session.get(QboTaxCode, changes.tax_code_id) is None:
            raise ValueError(f"tax code {changes.tax_code_id} does not exist")
        entry = replace(
            entry,
            lines=tuple(replace(line, tax_code_id=changes.tax_code_id) for line in entry.lines),
        )
    if changes.vendor_id:
        vendor = session.get(QboVendor, changes.vendor_id)
        if vendor is None:
            raise ValueError(f"vendor {changes.vendor_id} does not exist")
        entry = replace(entry, vendor_id=vendor.id, vendor_name=vendor.display_name)
    if changes.kind:
        entry = replace(entry, kind=changes.kind)
    if changes.invoice_date:
        from datetime import date

        entry = replace(entry, invoice_date=date.fromisoformat(changes.invoice_date))
    tags = (set(entry.tags) | set(changes.tags_add)) - set(changes.tags_remove)
    if unknown := tags - ALL_TAGS:
        raise ValueError(f"unknown tags {sorted(unknown)}")
    return replace(entry, tags=frozenset(tags))


def _recode_with_note(session: Session, doc: Document, note: str, actor: str) -> None:
    doc.summary = f"{doc.summary}\nReviewer ({actor}) says: {note}"
    doc.status = "ready_to_code"
    doc.question = None
    for p in session.scalars(
        select(Proposal).where(Proposal.document_id == doc.id, Proposal.status == "held")
    ):
        p.status = "superseded"
    jobs.enqueue(
        session,
        "code_document",
        {"document_id": doc.id},
        dedupe_key=f"code:{doc.id}:{utcnow().isoformat()}",
    )


def handle_reply(
    session: Session,
    email_id: int,
    client: AnthropicFoundry,
    sender: MailSender,
    settings: Settings,
) -> str:
    """Apply the reply and confirm to the reviewer. Returns a short description of the outcome."""
    email = session.get(InboundEmail, email_id)
    if email is None or email.status != "received":
        return "already handled"
    actor = email.sender_address
    token = parse_token(email.subject)
    question = None
    if token:
        question = session.scalar(
            select(Question)
            .where(Question.token == f"{token[0]}{token[1]}")
            .order_by(Question.id.desc())
            .limit(1)
        )

    def finish(outcome: str, action: str) -> str:
        email.status = "triaged"
        if question is not None:
            question.status = "answered" if action not in ("unclear", "error") else question.status
            question.answered_at = utcnow()
            question.answer_email_id = email.id
            question.answer_action = action
        audit.record(
            session,
            "reply.handled",
            "email",
            email.id,
            {"token": token, "action": action, "outcome": outcome},
            actor=actor,
        )
        sender.send([actor], f"Re: {email.subject}", outcome)
        return outcome

    if question is None:
        return finish(
            "I couldn't match your reply to an open question, so I haven't changed anything.",
            "error",
        )

    text = strip_quoted(email.body_text)
    action = quick_action(text) or interpret(client, settings, session, question, text)

    try:
        if question.proposal_id:
            proposal = session.get(Proposal, question.proposal_id)
            doc = session.get(Document, proposal.document_id)
            if proposal.status != "held":
                return finish(
                    f"P{proposal.id} is already {proposal.status}; nothing changed.", "error"
                )
            if action.action == "approve":
                posting.approve(session, proposal.id, actor)
                return finish(f"Approved P{proposal.id}; it will be posted shortly.", "approve")
            if action.action == "reject":
                posting.reject(session, proposal.id, actor, action.reason or "rejected by reply")
                return finish(f"Rejected P{proposal.id}. Nothing will be booked.", "reject")
            if action.action == "change" and action.changes:
                entry = _apply_changes(session, proposal, action.changes)
                new = record_proposal(
                    session,
                    doc,
                    entry,
                    settings,
                    rationale=f"Changed by {actor}: {text[:500]}",
                    approved_by=actor,
                    prompt_version=PROMPT_VERSION,
                )
                audit.record(
                    session,
                    "proposal.corrected",
                    "proposal",
                    new.id,
                    {
                        "from_proposal": proposal.id,
                        "changes": action.changes.model_dump(exclude_defaults=True),
                    },
                    actor=actor,
                )
                if new.status == "needs_fix":
                    problems = "; ".join(
                        f["message"] for f in new.findings if f["severity"] == "error"
                    )
                    return finish(
                        f"I made the change as P{new.id}, but it can't be booked: {problems}",
                        "change",
                    )
                return finish(
                    f"Changed and approved as P{new.id}; it will be posted shortly.", "change"
                )
            if action.action == "answer" and action.note:
                _recode_with_note(session, doc, action.note, actor)
                return finish(
                    "Thanks. I'll re-code it with that and come back to you if it still needs approval.",
                    "answer",
                )
        elif question.document_id:
            doc = session.get(Document, question.document_id)
            if doc.status != "needs_human":
                return finish(f"D{doc.id} is already {doc.status}; nothing changed.", "error")
            if action.action == "approve":
                _recode_with_note(session, doc, "Confirmed genuine; go ahead.", actor)
                return finish(
                    f"Thanks. I'll code D{doc.id} and book it if it passes the checks.", "approve"
                )
            if action.action == "reject":
                doc.status = "rejected"
                audit.record(
                    session,
                    "document.rejected",
                    "document",
                    doc.id,
                    {"reason": action.reason},
                    actor=actor,
                )
                return finish(f"OK, D{doc.id} won't be booked.", "reject")
            if action.action in ("answer", "change") and (action.note or action.changes):
                _recode_with_note(session, doc, action.note or text[:500], actor)
                return finish(f"Thanks. I'll code D{doc.id} with that.", "answer")
    except ValueError as err:
        return finish(
            f"I couldn't apply that ({err}). Nothing changed; could you rephrase?", "error"
        )

    return finish(
        action.note or "I wasn't sure what you meant; nothing changed. Could you rephrase?",
        "unclear",
    )
