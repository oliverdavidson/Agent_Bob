"""Step 3: propose how to book a document, then validate the proposal.

The model reads the document and proposes vendor, accounts, tax codes and tags, choosing
only from the QuickBooks cache. Code then converts amounts and dates strictly, runs the
validator, and decides what happens next:

- ambiguous, or the second opinion disagrees -> held, with the question for a person
- validator rejects -> needs_fix
- validator holds -> held
- validator passes or only flags -> approved by rule, and a posting job is queued
"""

import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib import resources
from typing import Literal

from anthropic import AnthropicFoundry
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit, jobs
from bob.accounting.context import build_context
from bob.accounting.entry import (
    ALL_TAGS,
    HOLD_TAGS,
    ProposedEntry,
    ProposedLine,
    entry_from_dict,
    entry_to_dict,
)
from bob.accounting.validate import invoice_key, normalise_vendor, validate
from bob.agent.triage import _attachment_blocks
from bob.config import Settings
from bob.db import utcnow
from bob.models import Attachment, Document, Proposal, QboAccount, QboBill, QboTaxCode, QboVendor
from bob.storage import Storage

log = logging.getLogger(__name__)

PROMPT_VERSION = "coding_v1"
_prompts = resources.files("bob.agent").joinpath("prompts")
SYSTEM_PROMPT = (
    _prompts.joinpath("coding_v1.md")
    .read_text()
    .replace("{policy}", _prompts.joinpath("coding_policy.md").read_text())
)
HISTORY_LIMIT = 10
# Proposals in these states count as "in the books" for duplicate checks.
LIVE_STATUSES = ("approved", "posting", "posted")

Tag = Literal[tuple(sorted(ALL_TAGS))]  # type: ignore[valid-type]


class CodedLine(BaseModel):
    description: str
    account_id: str
    amount: str
    tax_code_id: str
    tax_amount: str


class CodingResult(BaseModel):
    kind: Literal["bill", "expense", "credit"]
    vendor_name: str
    vendor_id: str | None
    invoice_number: str | None
    invoice_date: str
    due_date: str | None
    currency: str
    subtotal: str
    tax_total: str
    total: str
    supplier_tax_number: str | None
    service_start: str | None
    service_end: str | None
    lines: list[CodedLine]
    tags: list[Tag]
    rationale: str
    ambiguous: bool
    question: str | None


# --- context for the model ------------------------------------------------------------------


def reference_data(session: Session) -> str:
    accounts = session.scalars(
        select(QboAccount)
        .where(QboAccount.active.is_(True))
        .order_by(QboAccount.fully_qualified_name)
    ).all()
    tax_codes = session.scalars(select(QboTaxCode).where(QboTaxCode.active.is_(True))).all()
    vendors = session.scalars(
        select(QboVendor).where(QboVendor.active.is_(True)).order_by(QboVendor.display_name)
    ).all()
    lines = ["<chart_of_accounts>", "id | name | type"]
    lines += [f"{a.id} | {a.fully_qualified_name} | {a.account_type}" for a in accounts]
    lines += ["</chart_of_accounts>", "<tax_codes>", "id | name | purchase rate"]
    lines += [f"{t.id} | {t.name} | {Decimal(t.purchase_rate) * 100:.2f}%" for t in tax_codes]
    lines += ["</tax_codes>", "<vendors>", "id | name"]
    lines += [f"{v.id} | {v.display_name}" for v in vendors]
    lines += ["</vendors>", f"Allowed tags: {', '.join(sorted(ALL_TAGS))}"]
    return "\n".join(lines)


def vendor_history(session: Session, counterparty: str | None) -> str:
    if not counterparty:
        return "No vendor history (supplier not identified at triage)."
    key = normalise_vendor(counterparty)
    accounts = {a.id: a.fully_qualified_name for a in session.scalars(select(QboAccount))}
    rows = []
    bills = session.scalars(select(QboBill).order_by(QboBill.txn_date.desc())).all()
    for bill in bills:
        if normalise_vendor(bill.vendor_name or "") != key:
            continue
        coded = "; ".join(
            f"{accounts.get(line.get('account_id'), line.get('account_id'))} "
            f"{line.get('amount')} tax {line.get('tax_code_id')}"
            for line in bill.lines
        )
        rows.append(
            f"{bill.txn_date} {bill.entity} #{bill.doc_number or '-'} total {bill.total}: {coded}"
        )
        if len(rows) >= HISTORY_LIMIT:
            break
    if not rows:
        return f"No previous bookings found for {counterparty}."
    return f"Previous bookings for {counterparty} (newest first):\n" + "\n".join(rows)


def known_invoices(session: Session) -> tuple[frozenset, frozenset]:
    keys, hashes = set(), set()
    for bill in session.scalars(select(QboBill).where(QboBill.doc_number.is_not(None))):
        keys.add(invoice_key(bill.vendor_name or "", bill.doc_number))
    for p in session.scalars(select(Proposal).where(Proposal.status.in_(LIVE_STATUSES))):
        if p.entry.get("invoice_number"):
            keys.add(invoice_key(p.entry["vendor_name"], p.entry["invoice_number"]))
        if p.entry.get("document_sha256"):
            hashes.add(p.entry["document_sha256"])
    return frozenset(keys), frozenset(hashes)


# --- model call -----------------------------------------------------------------------------


def build_messages(
    session: Session, doc: Document, attachment: Attachment | None, data: bytes | None
) -> list[dict]:
    email = doc.email
    content: list[dict] = [
        {"type": "text", "text": reference_data(session)},
        {"type": "text", "text": vendor_history(session, doc.counterparty)},
        {
            "type": "text",
            "text": (
                f"Email from {email.sender_name or ''} <{email.sender_address}> "
                f"(trust: {email.sender_trust}), received {email.received_at.date().isoformat()}\n"
                f"Subject: {email.subject}\n<email_body>\n{email.body_text}\n</email_body>\n"
                f"Triage summary: {doc.summary}"
            ),
        },
    ]
    if attachment is not None and data is not None:
        content.extend(_attachment_blocks(0, attachment, data))
    content.append({"type": "text", "text": "Propose how to book this document."})
    return [{"role": "user", "content": content}]


def classify(
    client: AnthropicFoundry, settings: Settings, messages: list[dict]
) -> CodingResult | None:
    response = client.messages.parse(
        model=settings.model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.coding_effort},
        output_format=CodingResult,
    )
    if response.stop_reason == "refusal":
        log.warning("Coding refused: %s", response.stop_details)
        return None
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"Coding produced no usable output (stop_reason={response.stop_reason})")
    return response.parsed_output


# --- conversion and comparison -------------------------------------------------------------


def _money(value: str, field: str) -> Decimal:
    try:
        amount = Decimal(value.replace(",", "").replace("$", "").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field} is not an amount: {value!r}") from None
    if not amount.is_finite():
        raise ValueError(f"{field} is not an amount: {value!r}")
    return amount


def _date(value: str | None, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} is not a YYYY-MM-DD date: {value!r}") from None


def to_entry(result: CodingResult, document_sha256: str | None) -> ProposedEntry:
    invoice_date = _date(result.invoice_date, "invoice_date")
    if invoice_date is None:
        raise ValueError("invoice_date is missing")
    return ProposedEntry(
        kind=result.kind,
        vendor_name=result.vendor_name.strip(),
        vendor_id=result.vendor_id or None,
        invoice_number=(result.invoice_number or "").strip() or None,
        invoice_date=invoice_date,
        currency=result.currency.strip().upper(),
        subtotal=_money(result.subtotal, "subtotal"),
        tax_total=_money(result.tax_total, "tax_total"),
        total=_money(result.total, "total"),
        lines=tuple(
            ProposedLine(
                account_id=line.account_id,
                amount=_money(line.amount, "line amount"),
                tax_code_id=line.tax_code_id,
                tax_amount=_money(line.tax_amount, "line tax"),
                description=line.description,
            )
            for line in result.lines
        ),
        document_sha256=document_sha256,
        supplier_tax_number=result.supplier_tax_number,
        due_date=_date(result.due_date, "due_date"),
        service_start=_date(result.service_start, "service_start"),
        service_end=_date(result.service_end, "service_end"),
        tags=frozenset(result.tags),
    )


def disagreements(a: ProposedEntry, b: ProposedEntry) -> list[str]:
    """Differences between two independent proposals that matter for the books."""
    out = []
    if a.kind != b.kind:
        out.append(f"kind {a.kind} vs {b.kind}")
    if a.total != b.total:
        out.append(f"total {a.total} vs {b.total}")
    accounts_a = sorted({line.account_id for line in a.lines})
    accounts_b = sorted({line.account_id for line in b.lines})
    if accounts_a != accounts_b:
        out.append(f"accounts {accounts_a} vs {accounts_b}")
    taxes_a = sorted({line.tax_code_id for line in a.lines})
    taxes_b = sorted({line.tax_code_id for line in b.lines})
    if taxes_a != taxes_b:
        out.append(f"tax codes {taxes_a} vs {taxes_b}")
    if (a.tags & HOLD_TAGS) != (b.tags & HOLD_TAGS):
        out.append(f"hold tags {sorted(a.tags & HOLD_TAGS)} vs {sorted(b.tags & HOLD_TAGS)}")
    return out


def needs_second_opinion(entry: ProposedEntry, history: str) -> bool:
    """Routine items with history go straight through; everything else is checked twice."""
    return history.startswith("No ") or entry.vendor_id is None or bool(entry.tags & HOLD_TAGS)


# --- the step -------------------------------------------------------------------------------


def next_version(session: Session, document_id: int) -> int:
    versions = session.scalars(select(Proposal.version).where(Proposal.document_id == document_id))
    return max(versions, default=0) + 1


def record_proposal(
    session: Session,
    doc: Document,
    entry: ProposedEntry,
    settings: Settings,
    *,
    rationale: str,
    question: str | None = None,
    second_opinion: dict | None = None,
    force_hold: bool = False,
    approved_by: str | None = None,
    today: date | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
) -> Proposal:
    """Validate an entry and store it as a new proposal version, queueing posting if allowed.

    `approved_by` set means a person approved it: a validator hold no longer stops it, but a
    rejection still does.
    """
    today = today or utcnow().date()
    keys, hashes = known_invoices(session)
    ctx = build_context(session, today, settings.materiality, keys, hashes)
    result = validate(entry, ctx)

    if result.outcome == "reject":
        status = "needs_fix"
    elif approved_by:
        status = "approved"
    elif force_hold or result.outcome == "hold":
        status = "held"
    else:
        status = "approved"
        approved_by = "rule"

    for older in session.scalars(
        select(Proposal).where(
            Proposal.document_id == doc.id, Proposal.status.in_(("held", "needs_fix", "proposed"))
        )
    ):
        older.status = "superseded"

    proposal = Proposal(
        document_id=doc.id,
        version=next_version(session, doc.id),
        status=status,
        approved_by=approved_by if status == "approved" else None,
        approved_at=utcnow() if status == "approved" else None,
        entry=entry_to_dict(entry),
        outcome=result.outcome,
        findings=[f.__dict__ for f in result.findings],
        rationale=rationale,
        question=question,
        second_opinion=second_opinion,
        model_name=model_name,
        prompt_version=prompt_version,
    )
    session.add(proposal)
    session.flush()
    proposal.request_id = f"bob-{proposal.id}"
    doc.status = "coded"
    audit.record(
        session,
        "proposal.created",
        "proposal",
        proposal.id,
        {
            "document_id": doc.id,
            "version": proposal.version,
            "status": status,
            "outcome": result.outcome,
            "findings": result.codes(),
            "approved_by": proposal.approved_by,
            "total": str(entry.total),
            "vendor": entry.vendor_name,
        },
    )
    if status == "approved":
        jobs.enqueue(
            session, "post_proposal", {"proposal_id": proposal.id}, dedupe_key=f"post:{proposal.id}"
        )
    return proposal


def code_document(
    session: Session,
    document_id: int,
    client: AnthropicFoundry,
    storage: Storage,
    settings: Settings,
    today: date | None = None,
) -> Proposal | None:
    doc = session.get(Document, document_id)
    if doc is None or doc.status != "ready_to_code":
        return None  # already handled, or not something to book

    attachment = session.get(Attachment, doc.attachment_id) if doc.attachment_id else None
    data = storage.get(attachment.blob_path) if attachment else None
    messages = build_messages(session, doc, attachment, data)
    history = vendor_history(session, doc.counterparty)
    sha = attachment.sha256 if attachment else None

    first = classify(client, settings, messages)
    if first is None:
        doc.status = "needs_human"
        doc.question = "Bob could not propose a booking for this document. How should it be booked?"
        audit.record(session, "coding.refused", "document", doc.id)
        return None
    try:
        entry = to_entry(first, sha)
    except ValueError as err:
        doc.status = "needs_human"
        doc.question = (
            f"Bob could not read this document reliably ({err}). How should it be booked?"
        )
        audit.record(session, "coding.unreadable", "document", doc.id, {"error": str(err)})
        return None

    question = first.question if first.ambiguous else None
    second: dict | None = None
    if not first.ambiguous and needs_second_opinion(entry, history):
        other = classify(client, settings, messages)
        if other is not None:
            try:
                differences = disagreements(entry, to_entry(other, sha))
            except ValueError as err:
                differences = [f"second reading unreadable: {err}"]
            second = {"result": other.model_dump(), "differences": differences}
            if differences:
                question = (
                    "Two independent readings of this document disagree ("
                    + "; ".join(differences)
                    + "). Which is right?"
                )

    # Model-supplied vendor ids must exist; otherwise treat as a new vendor.
    if entry.vendor_id and session.get(QboVendor, entry.vendor_id) is None:
        entry = replace(entry, vendor_id=None)

    return record_proposal(
        session,
        doc,
        entry,
        settings,
        rationale=first.rationale,
        question=question,
        second_opinion=second,
        force_hold=question is not None,
        today=today,
        model_name=settings.model,
        prompt_version=PROMPT_VERSION,
    )


def proposal_entry(proposal: Proposal) -> ProposedEntry:
    return entry_from_dict(proposal.entry)
