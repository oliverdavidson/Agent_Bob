"""Step 5: post approved proposals to QuickBooks, and undo them.

Safety, in order of the checks below:
1. The kill switch (controls.posting_paused) stops all posting.
2. Entries approved by rule are capped per day by count and amount; a person's approval
   bypasses the cap.
3. The entry is validated again against fresh QuickBooks data (the closing date or a
   duplicate may have appeared since it was coded).
4. Every create carries the proposal's request id, so a retry after a crash returns the
   original transaction instead of creating a second one.

Undo deletes the QuickBooks transaction Bob created, refusing if it sits in a closed period
or a payment has been applied to it. QuickBooks keeps its own audit log of the deletion.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit
from bob.accounting.context import build_context
from bob.accounting.entry import ProposedEntry, entry_from_dict, entry_to_dict
from bob.accounting.validate import validate
from bob.agent.coding import known_invoices
from bob.config import Settings
from bob.db import utcnow
from bob.models import (
    Attachment,
    Control,
    Document,
    Proposal,
    QboSetting,
    QboVendor,
    Question,
)
from bob.qbo.client import NotConnected, QBOClient, QBOError, WritesDisabled
from bob.storage import Storage

log = logging.getLogger(__name__)

ENTITY_FOR_KIND = {"bill": "Bill", "credit": "VendorCredit", "expense": "Purchase"}


# --- kill switch ----------------------------------------------------------------------------


def posting_paused(session: Session) -> bool:
    control = session.get(Control, "posting_paused")
    return control is not None and control.value == "true"


def set_posting_paused(session: Session, paused: bool, actor: str) -> None:
    session.merge(
        Control(key="posting_paused", value="true" if paused else "false", updated_by=actor)
    )
    audit.record(
        session, "posting.paused" if paused else "posting.resumed", "control", None, actor=actor
    )


# --- payloads -------------------------------------------------------------------------------


def _note(proposal: Proposal) -> str:
    return f"Posted by Bob · proposal {proposal.id} v{proposal.version}"


def build_payload(entry: ProposedEntry, proposal: Proposal, settings: Settings, home: str) -> dict:
    lines = [
        {
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": float(line.amount),
            "Description": line.description[:4000],
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": line.account_id},
                "TaxCodeRef": {"value": line.tax_code_id},
            },
        }
        for line in entry.lines
    ]
    payload: dict = {
        "TxnDate": entry.invoice_date.isoformat(),
        "PrivateNote": _note(proposal),
        "GlobalTaxCalculation": "TaxExcluded",
        "Line": lines,
        "TxnTaxDetail": {"TotalTax": float(entry.tax_total)},
    }
    if entry.currency != home:
        payload["CurrencyRef"] = {"value": entry.currency}
    if entry.kind == "expense":
        payload["AccountRef"] = {"value": settings.expense_payment_account_id}
        payload["PaymentType"] = settings.expense_payment_type
        payload["EntityRef"] = {"value": entry.vendor_id, "type": "Vendor"}
        if entry.invoice_number:
            payload["DocNumber"] = entry.invoice_number[:21]
    else:
        payload["VendorRef"] = {"value": entry.vendor_id}
        if entry.invoice_number:
            payload["DocNumber"] = entry.invoice_number[:21]
        if entry.kind == "bill" and entry.due_date:
            payload["DueDate"] = entry.due_date.isoformat()
    return payload


# --- posting --------------------------------------------------------------------------------


def _hold(session: Session, proposal: Proposal, question: str, code: str) -> None:
    proposal.status = "held"
    proposal.question = question
    proposal.findings = [
        *proposal.findings,
        {"code": code, "severity": "hold", "message": question},
    ]
    audit.record(session, "proposal.held", "proposal", proposal.id, {"reason": code})


def _posted_last_24h(session: Session) -> tuple[int, Decimal]:
    """Rule-approved entries posted in the last 24 hours (a rolling window, not a calendar day)."""
    rows = session.scalars(
        select(Proposal).where(
            Proposal.status.in_(("posted", "posting")),
            Proposal.approved_by == "rule",
            Proposal.posted_at >= utcnow() - timedelta(hours=24),
        )
    ).all()
    return len(rows), sum((Decimal(p.entry["total"]) for p in rows), Decimal(0))


def _ensure_vendor(
    session: Session, proposal: Proposal, entry: ProposedEntry, qbo: QBOClient
) -> str:
    if entry.vendor_id:
        return entry.vendor_id
    vendor = qbo.create(
        "Vendor",
        {"DisplayName": entry.vendor_name[:500]},
        request_id=f"{proposal.request_id}-vendor",
    )
    session.merge(QboVendor(id=vendor["Id"], display_name=vendor["DisplayName"]))
    proposal.entry = {**proposal.entry, "vendor_id": vendor["Id"]}  # new dict: tracked change
    audit.record(
        session,
        "qbo.vendor_created",
        "proposal",
        proposal.id,
        {"vendor_id": vendor["Id"], "name": entry.vendor_name},
    )
    return vendor["Id"]


def post_proposal(
    session: Session,
    proposal_id: int,
    qbo: QBOClient,
    storage: Storage,
    settings: Settings,
    today: date | None = None,
) -> None:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None or proposal.status not in ("approved", "posting"):
        return  # posted already, or no longer approved
    today = today or utcnow().date()
    entry = entry_from_dict(proposal.entry)
    by_rule = proposal.approved_by == "rule"

    if posting_paused(session):
        proposal.last_error = "Posting is paused."
        log.info("Posting paused; proposal %s left approved", proposal.id)
        return

    if by_rule and proposal.status == "approved":
        count, amount = _posted_last_24h(session)
        if (
            count + 1 > settings.daily_post_cap_count
            or amount + entry.total > settings.daily_post_cap_amount
        ):
            _hold(
                session,
                proposal,
                "Bob's daily posting limit is reached. Post this one?",
                "daily_cap",
            )
            return

    if entry.kind == "expense" and not settings.expense_payment_account_id:
        _hold(
            session,
            proposal,
            "Which account paid for this receipt? (BOB_EXPENSE_PAYMENT_ACCOUNT_ID is not set.)",
            "no_payment_account",
        )
        return

    keys, hashes = known_invoices(session, exclude_id=proposal.id)
    result = validate(entry, build_context(session, today, settings.materiality, keys, hashes))
    if result.outcome == "reject":
        proposal.status = "needs_fix"
        proposal.findings = [f.__dict__ for f in result.findings]
        audit.record(
            session,
            "proposal.revalidated",
            "proposal",
            proposal.id,
            {"outcome": "reject", "findings": result.codes()},
        )
        return
    if result.outcome == "hold" and by_rule:
        proposal.outcome = "hold"
        proposal.findings = [f.__dict__ for f in result.findings]
        _hold(
            session,
            proposal,
            "Something changed since this was coded; please review.",
            "revalidation_hold",
        )
        return

    # Record the attempt before calling QuickBooks. A crash after this point is retried with
    # the same request id, which QuickBooks treats as the same request.
    proposal.status = "posting"
    session.commit()

    try:
        vendor_id = _ensure_vendor(session, proposal, entry, qbo)
        entry = entry_from_dict({**entry_to_dict(entry), "vendor_id": vendor_id})
        home = (session.get(QboSetting, "home_currency") or QboSetting(value="CAD")).value or "CAD"
        entity = ENTITY_FOR_KIND[entry.kind]
        created = qbo.create(
            entity, build_payload(entry, proposal, settings, home), request_id=proposal.request_id
        )
    except (WritesDisabled, NotConnected) as err:
        proposal.status = "approved"
        proposal.last_error = str(err)
        log.warning("Proposal %s not posted: %s", proposal.id, err)
        return
    except QBOError as err:
        if err.status >= 500:
            raise  # transient: let the job retry with the same request id
        proposal.status = "failed"
        proposal.last_error = str(err)
        audit.record(session, "proposal.failed", "proposal", proposal.id, {"error": str(err)})
        return

    proposal.status = "posted"
    proposal.qbo_entity = entity
    proposal.qbo_id = created["Id"]
    proposal.posted_at = utcnow()
    proposal.last_error = None
    doc = session.get(Document, proposal.document_id)
    doc.status = "posted"
    audit.record(
        session,
        "proposal.posted",
        "proposal",
        proposal.id,
        {
            "entity": entity,
            "qbo_id": created["Id"],
            "total": str(entry.total),
            "approved_by": proposal.approved_by,
        },
    )
    _attach_document(session, proposal, doc, qbo, storage)


def _attach_document(
    session: Session, proposal: Proposal, doc: Document, qbo: QBOClient, storage: Storage
) -> None:
    if not doc.attachment_id:
        return
    att = session.get(Attachment, doc.attachment_id)
    try:
        qbo.attach(
            proposal.qbo_entity,
            proposal.qbo_id,
            att.filename,
            att.content_type,
            storage.get(att.blob_path),
            request_id=f"{proposal.request_id}-attach",
        )
        audit.record(session, "qbo.attached", "proposal", proposal.id, {"file": att.filename})
    except (QBOError, WritesDisabled) as err:
        # The entry is posted; a missing attachment is reported, not retried blindly.
        audit.record(session, "qbo.attach_failed", "proposal", proposal.id, {"error": str(err)})


def _close_questions(session: Session, proposal_id: int) -> None:
    for q in session.scalars(
        select(Question).where(Question.proposal_id == proposal_id, Question.status == "open")
    ):
        q.status = "closed"


def approve(session: Session, proposal_id: int, actor: str) -> Proposal:
    """A person approves a held proposal. Validator rejections cannot be approved."""
    from bob import jobs

    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise ValueError(f"No proposal {proposal_id}")
    if proposal.status != "held":
        raise ValueError(f"Proposal {proposal_id} is {proposal.status}, not held")
    proposal.status = "approved"
    proposal.approved_by = actor
    proposal.approved_at = utcnow()
    _close_questions(session, proposal.id)
    audit.record(session, "proposal.approved", "proposal", proposal.id, actor=actor)
    jobs.enqueue(
        session,
        "post_proposal",
        {"proposal_id": proposal.id},
        dedupe_key=f"post:{proposal.id}:{actor}",
    )
    return proposal


def reject(session: Session, proposal_id: int, actor: str, reason: str) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None or proposal.status not in ("held", "needs_fix", "approved"):
        raise ValueError(f"Proposal {proposal_id} cannot be rejected")
    proposal.status = "rejected"
    session.get(Document, proposal.document_id).status = "rejected"
    _close_questions(session, proposal.id)
    audit.record(
        session, "proposal.rejected", "proposal", proposal.id, {"reason": reason}, actor=actor
    )
    return proposal


# --- undo -----------------------------------------------------------------------------------


class UndoRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class UndoResult:
    proposal_id: int
    done: bool
    message: str


def reverse(
    session: Session, proposal_id: int, qbo: QBOClient, actor: str, reason: str
) -> UndoResult:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None or proposal.status != "posted":
        raise UndoRefused(f"Proposal {proposal_id} is not posted")
    closing = (session.get(QboSetting, "closing_date") or QboSetting(value=None)).value
    if closing and proposal.entry["invoice_date"] <= closing:
        raise UndoRefused(
            f"Dated {proposal.entry['invoice_date']}, inside the closed period (closing date {closing}). "
            "Closed periods are only reopened by a person in QuickBooks."
        )
    current = qbo.read(proposal.qbo_entity, proposal.qbo_id)
    if proposal.qbo_entity == "Bill" and Decimal(str(current.get("Balance", 0))) != Decimal(
        str(current.get("TotalAmt", 0))
    ):
        raise UndoRefused("A payment has been applied to this bill; remove the payment first.")
    qbo.delete(
        proposal.qbo_entity,
        proposal.qbo_id,
        current["SyncToken"],
        request_id=f"{proposal.request_id}-undo",
    )
    proposal.status = "reversed"
    proposal.reversed_at = utcnow()
    session.get(Document, proposal.document_id).status = "reversed"
    audit.record(
        session,
        "proposal.reversed",
        "proposal",
        proposal.id,
        {"entity": proposal.qbo_entity, "qbo_id": proposal.qbo_id, "reason": reason},
        actor=actor,
    )
    return UndoResult(proposal.id, True, f"Deleted {proposal.qbo_entity} {proposal.qbo_id}")


def find_posted(
    session: Session,
    *,
    since: date | None = None,
    vendor: str | None = None,
    ids: list[int] | None = None,
) -> list[Proposal]:
    """Posted proposals matching a filter, for bulk undo (e.g. everything from one vendor)."""
    from bob.accounting.validate import normalise_vendor

    stmt = select(Proposal).where(Proposal.status == "posted").order_by(Proposal.id)
    if ids:
        stmt = stmt.where(Proposal.id.in_(ids))
    rows = session.scalars(stmt).all()
    if since:
        rows = [p for p in rows if p.posted_at and p.posted_at.date() >= since]
    if vendor:
        key = normalise_vendor(vendor)
        rows = [p for p in rows if normalise_vendor(p.entry["vendor_name"]) == key]
    return rows
