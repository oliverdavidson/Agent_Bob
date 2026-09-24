from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bob.agent.coding import CodedLine, CodingResult, code_document, to_entry, vendor_history
from bob.models import (
    Attachment,
    AuditEvent,
    Document,
    InboundEmail,
    Job,
    Proposal,
    QboAccount,
    QboBill,
    QboSetting,
    QboTaxCode,
    QboVendor,
)
from tests.conftest import PDF_BYTES, FakeClaude

TODAY = date(2026, 10, 15)


def seed_cache(s):
    s.add_all(
        [
            QboAccount(
                id="60", name="Software", fully_qualified_name="Software", account_type="Expense"
            ),
            QboAccount(
                id="61",
                name="Legal Fees",
                fully_qualified_name="Legal Fees",
                account_type="Expense",
            ),
            QboAccount(
                id="65",
                name="Deal Costs",
                fully_qualified_name="Deal Costs",
                account_type="Expense",
            ),
            QboAccount(
                id="13",
                name="Prepaid",
                fully_qualified_name="Prepaid Expenses",
                account_type="Other Current Asset",
            ),
            QboTaxCode(id="4", name="GST", purchase_rate=Decimal("0.05")),
            QboTaxCode(id="2", name="Exempt", purchase_rate=Decimal(0)),
            QboVendor(id="17", display_name="Northwind Cloud Software Inc."),
            QboSetting(key="closing_date", value="2026-09-30"),
            QboSetting(key="home_currency", value="CAD"),
            QboBill(
                id="Bill:900",
                entity="Bill",
                vendor_id="17",
                vendor_name="Northwind Cloud Software Inc.",
                doc_number="NW-10311",
                txn_date="2026-09-01",
                total=Decimal("1260.00"),
                lines=[{"account_id": "60", "amount": "1200.00", "tax_code_id": "4"}],
            ),
        ]
    )


def make_document(s, storage, counterparty="Northwind Cloud Software Inc.", status="ready_to_code"):
    email = InboundEmail(
        graph_message_id=f"g-{uuid4()}",
        sender_address="billing@northwindcloud.ca",
        sender_trust="known",
        subject="Invoice",
        body_text="Invoice attached.",
        received_at=datetime(2026, 10, 3, tzinfo=UTC),
        status="triaged",
    )
    storage.put("attachments/aa/inv.pdf", PDF_BYTES, "application/pdf")
    att = Attachment(
        filename="inv.pdf",
        content_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        sha256="f" * 64,
        blob_path="attachments/aa/inv.pdf",
    )
    email.attachments.append(att)
    s.add(email)
    s.flush()
    doc = Document(
        email=email,
        attachment_id=att.id,
        doc_type="vendor_invoice",
        status=status,
        counterparty=counterparty,
        summary="Northwind invoice",
    )
    s.add(doc)
    s.flush()
    return doc.id


def result(**overrides) -> CodingResult:
    base = {
        "kind": "bill",
        "vendor_name": "Northwind Cloud Software Inc.",
        "vendor_id": "17",
        "invoice_number": "NW-10388",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-31",
        "currency": "CAD",
        "subtotal": "1200.00",
        "tax_total": "60.00",
        "total": "1260.00",
        "supplier_tax_number": "812345678 RT0001",
        "service_start": None,
        "service_end": None,
        "lines": [
            CodedLine(
                description="Subscription",
                account_id="60",
                amount="1200.00",
                tax_code_id="4",
                tax_amount="60.00",
            )
        ],
        "tags": [],
        "rationale": "Monthly subscription, same as prior Northwind bills.",
        "ambiguous": False,
        "question": None,
    }
    base.update(overrides)
    return CodingResult(**base)


@pytest.fixture
def doc_id(factory, storage):
    with factory() as s:
        seed_cache(s)
        doc_id = make_document(s, storage)
        s.commit()
    return doc_id


def run(factory, storage, settings, doc_id, claude):
    with factory() as s:
        proposal = code_document(s, doc_id, claude, storage, settings, today=TODAY)
        s.commit()
        return proposal.id if proposal else None


def test_routine_invoice_is_approved_and_queued_for_posting(factory, storage, settings, doc_id):
    claude = FakeClaude(result())
    pid = run(factory, storage, settings, doc_id, claude)

    assert len(claude.calls) == 1  # history exists, known vendor: no second opinion
    with factory() as s:
        p = s.get(Proposal, pid)
        assert (p.status, p.outcome, p.approved_by) == ("approved", "post", "rule")
        assert p.request_id == f"bob-{pid}"
        assert s.get(Document, doc_id).status == "coded"
        job = s.scalars(select(Job).where(Job.kind == "post_proposal")).one()
        assert job.payload == {"proposal_id": pid}
        assert s.scalar(select(AuditEvent.action).where(AuditEvent.action == "proposal.created"))
    content = claude.calls[0]["messages"][0]["content"]
    assert "60 | Software | Expense" in content[0]["text"]
    assert "NW-10311" in content[1]["text"]  # vendor history
    assert claude.calls[0]["output_config"] == {"effort": "high"}


def test_ambiguous_is_held_with_question(factory, storage, settings, doc_id):
    claude = FakeClaude(result(ambiguous=True, question="Is this for BridgeWerk or Aurora?"))
    pid = run(factory, storage, settings, doc_id, claude)
    with factory() as s:
        p = s.get(Proposal, pid)
        assert p.status == "held"
        assert p.question == "Is this for BridgeWerk or Aurora?"
        assert s.scalars(select(Job).where(Job.kind == "post_proposal")).first() is None


def test_acquisition_tag_gets_second_opinion_and_disagreement_holds(
    factory, storage, settings, doc_id
):
    first = result(
        tags=["acquisition_related"],
        lines=[
            CodedLine(
                description="Falcon",
                account_id="65",
                amount="1200.00",
                tax_code_id="4",
                tax_amount="60.00",
            )
        ],
    )
    second = result(
        lines=[
            CodedLine(
                description="General",
                account_id="61",
                amount="1200.00",
                tax_code_id="4",
                tax_amount="60.00",
            )
        ]
    )
    claude = FakeClaude([first, second])
    pid = run(factory, storage, settings, doc_id, claude)

    assert len(claude.calls) == 2
    with factory() as s:
        p = s.get(Proposal, pid)
        assert p.status == "held"
        assert "disagree" in p.question
        assert "accounts ['65'] vs ['61']" in p.second_opinion["differences"]


def test_agreeing_second_opinion_still_holds_for_hold_tag(factory, storage, settings, doc_id):
    tagged = result(tags=["acquisition_related"])
    claude = FakeClaude([tagged, tagged])
    pid = run(factory, storage, settings, doc_id, claude)
    with factory() as s:
        p = s.get(Proposal, pid)
        assert p.second_opinion["differences"] == []
        assert (p.status, p.outcome) == ("held", "hold")  # the tag itself holds


def test_validator_rejection_needs_fix(factory, storage, settings, doc_id):
    claude = FakeClaude(result(total="1265.00"))
    pid = run(factory, storage, settings, doc_id, claude)
    with factory() as s:
        p = s.get(Proposal, pid)
        assert (p.status, p.outcome) == ("needs_fix", "reject")
        assert "total_mismatch" in [f["code"] for f in p.findings]


def test_duplicate_of_cached_qbo_bill_is_rejected(factory, storage, settings, doc_id):
    claude = FakeClaude(result(invoice_number="NW 10311"))
    pid = run(factory, storage, settings, doc_id, claude)
    with factory() as s:
        assert "duplicate_invoice" in [f["code"] for f in s.get(Proposal, pid).findings]


def test_unreadable_amount_goes_to_a_person(factory, storage, settings, doc_id):
    claude = FakeClaude(result(total="twelve hundred"))
    assert run(factory, storage, settings, doc_id, claude) is None
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "needs_human"
        assert "total is not an amount" in doc.question


def test_invented_vendor_id_becomes_new_vendor(factory, storage, settings, doc_id):
    claude = FakeClaude([result(vendor_id="999"), result(vendor_id="999")])
    pid = run(factory, storage, settings, doc_id, claude)
    with factory() as s:
        p = s.get(Proposal, pid)
        assert p.entry["vendor_id"] is None
        assert "new_vendor" in [f["code"] for f in p.findings]
        assert p.status == "approved"  # new vendor is a flag, not a hold


def test_document_not_ready_is_skipped(factory, storage, settings):
    with factory() as s:
        seed_cache(s)
        doc_id = make_document(s, storage, status="needs_human")
        s.commit()
    claude = FakeClaude(result())
    assert run(factory, storage, settings, doc_id, claude) is None
    assert claude.calls == []


def test_money_parsing_is_strict():
    entry = to_entry(result(subtotal="1,200.00", total="$1,260.00"), None)
    assert entry.subtotal == Decimal("1200.00") and entry.total == Decimal("1260.00")
    with pytest.raises(ValueError, match="invoice_date"):
        to_entry(result(invoice_date="Oct 1 2026"), None)


def test_vendor_history_matches_name_variants(factory):
    with factory() as s:
        seed_cache(s)
        s.commit()
        assert "NW-10311" in vendor_history(s, "NORTHWIND CLOUD SOFTWARE")
        assert vendor_history(s, "Someone Else").startswith("No previous bookings")
