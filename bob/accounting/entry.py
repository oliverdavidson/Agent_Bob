"""The shape of a proposed accounting entry: what the coding step produces and the validator
checks. Money is Decimal throughout; amounts are positive, and `kind` says which way they go."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

EntryKind = Literal["bill", "expense", "credit"]

# Tags the coding step may attach. Hold tags stop posting; flag tags post and go on the
# confirm-before-close list.
HOLD_TAGS = frozenset(
    {"related_party", "intercompany", "acquisition_related", "bank_details_changed"}
)
FLAG_TAGS = frozenset({"capex", "personal", "unusual"})
ALL_TAGS = HOLD_TAGS | FLAG_TAGS


@dataclass(frozen=True)
class ProposedLine:
    account_id: str
    amount: Decimal  # before tax
    tax_code_id: str
    tax_amount: Decimal
    description: str = ""


@dataclass(frozen=True)
class ProposedEntry:
    kind: EntryKind
    vendor_name: str
    vendor_id: str | None  # None when the vendor does not exist in QBO yet
    invoice_number: str | None
    invoice_date: date
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: tuple[ProposedLine, ...]
    document_sha256: str | None = None
    supplier_tax_number: str | None = None  # GST/HST registration number on the invoice
    due_date: date | None = None
    service_start: date | None = None
    service_end: date | None = None
    tags: frozenset[str] = field(default_factory=frozenset)


def entry_to_dict(entry: ProposedEntry) -> dict:
    def s(value):
        return None if value is None else str(value)

    return {
        "kind": entry.kind,
        "vendor_name": entry.vendor_name,
        "vendor_id": entry.vendor_id,
        "invoice_number": entry.invoice_number,
        "invoice_date": entry.invoice_date.isoformat(),
        "currency": entry.currency,
        "subtotal": str(entry.subtotal),
        "tax_total": str(entry.tax_total),
        "total": str(entry.total),
        "lines": [
            {
                "account_id": line.account_id,
                "amount": str(line.amount),
                "tax_code_id": line.tax_code_id,
                "tax_amount": str(line.tax_amount),
                "description": line.description,
            }
            for line in entry.lines
        ],
        "document_sha256": entry.document_sha256,
        "supplier_tax_number": entry.supplier_tax_number,
        "due_date": s(entry.due_date),
        "service_start": s(entry.service_start),
        "service_end": s(entry.service_end),
        "tags": sorted(entry.tags),
    }


def entry_from_dict(data: dict) -> ProposedEntry:
    def d(value):
        return None if value is None else date.fromisoformat(value)

    return ProposedEntry(
        kind=data["kind"],
        vendor_name=data["vendor_name"],
        vendor_id=data.get("vendor_id"),
        invoice_number=data.get("invoice_number"),
        invoice_date=date.fromisoformat(data["invoice_date"]),
        currency=data["currency"],
        subtotal=Decimal(data["subtotal"]),
        tax_total=Decimal(data["tax_total"]),
        total=Decimal(data["total"]),
        lines=tuple(
            ProposedLine(
                account_id=line["account_id"],
                amount=Decimal(line["amount"]),
                tax_code_id=line["tax_code_id"],
                tax_amount=Decimal(line["tax_amount"]),
                description=line.get("description", ""),
            )
            for line in data["lines"]
        ),
        document_sha256=data.get("document_sha256"),
        supplier_tax_number=data.get("supplier_tax_number"),
        due_date=d(data.get("due_date")),
        service_start=d(data.get("service_start")),
        service_end=d(data.get("service_end")),
        tags=frozenset(data.get("tags", [])),
    )
