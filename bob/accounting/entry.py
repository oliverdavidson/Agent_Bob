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
