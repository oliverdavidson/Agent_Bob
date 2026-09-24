"""Deterministic checks every proposed entry passes before it can post.

The model proposes; this module decides. Each check returns findings at one of three levels:

- error: the entry is wrong or must not exist (bad arithmetic, duplicate, unknown account).
  It cannot post as proposed.
- hold: the entry may be right but a person approves it before it posts.
- flag: the entry posts, and goes on the confirm-before-close list.

The outcome is the most severe level found.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from bob.accounting.entry import ALL_TAGS, FLAG_TAGS, HOLD_TAGS, ProposedEntry

Severity = Literal["error", "hold", "flag"]
Outcome = Literal["reject", "hold", "post_and_flag", "post"]

CENT = Decimal("0.01")
# CRA requires the supplier's GST/HST registration number on invoices of $100 or more
# (taxes included) to support an input tax credit. Confirm with the accountant.
ITC_NUMBER_THRESHOLD = Decimal(100)
GST_NUMBER = re.compile(r"^\d{9}\s*RT\s*\d{4}$", re.IGNORECASE)
# Expense-type QBO accounts. A long service period booked to one of these may be a prepaid.
EXPENSE_ACCOUNT_TYPES = frozenset({"Expense", "Other Expense", "Cost of Goods Sold"})
PREPAID_REVIEW_DAYS = 92


@dataclass(frozen=True)
class AccountInfo:
    id: str
    name: str
    account_type: str
    active: bool = True


@dataclass(frozen=True)
class TaxCodeInfo:
    id: str
    name: str
    purchase_rate: Decimal  # e.g. Decimal("0.05") for GST
    active: bool = True


@dataclass(frozen=True)
class ValidationContext:
    accounts: dict[str, AccountInfo]
    tax_codes: dict[str, TaxCodeInfo]
    closing_date: date | None
    today: date
    materiality: Decimal
    home_currency: str = "CAD"
    # Normalised (vendor, invoice number) pairs already in QBO or already proposed.
    existing_invoices: frozenset[tuple[str, str]] = frozenset()
    # Hashes of documents already posted.
    posted_hashes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def outcome(self) -> Outcome:
        levels = {f.severity for f in self.findings}
        if "error" in levels:
            return "reject"
        if "hold" in levels:
            return "hold"
        if "flag" in levels:
            return "post_and_flag"
        return "post"

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]


def normalise_vendor(name: str) -> str:
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    name = re.sub(r"\b(and|the|inc|ltd|llp|llc|corp|corporation|limited|co)\b", " ", name)
    return " ".join(name.split())


def normalise_invoice_number(number: str) -> str:
    """INV-0042, inv 42 and INV00042 all become INV42."""
    compact = re.sub(r"[^A-Z0-9]", "", number.upper())
    return re.sub(r"(?<!\d)0+(?=\d)", "", compact) or "0"


def invoice_key(vendor_name: str, invoice_number: str) -> tuple[str, str]:
    return normalise_vendor(vendor_name), normalise_invoice_number(invoice_number)


def validate(entry: ProposedEntry, ctx: ValidationContext) -> ValidationResult:
    result = ValidationResult()
    add = result.findings.append
    for check in (
        _arithmetic,
        _references,
        _tax,
        _duplicates,
        _dates,
        _amount_and_currency,
        _vendor,
        _tags,
        _prepaid,
    ):
        for finding in check(entry, ctx):
            add(finding)
    return result


def _arithmetic(e: ProposedEntry, ctx: ValidationContext):
    if not e.lines:
        yield Finding("no_lines", "error", "The entry has no lines.")
        return
    for amount in (e.subtotal, e.tax_total, e.total):
        if amount != amount.quantize(CENT):
            yield Finding("fractional_cents", "error", f"{amount} is not a whole number of cents.")
    if any(line.amount <= 0 or line.tax_amount < 0 for line in e.lines):
        yield Finding(
            "non_positive_line", "error", "Line amounts must be positive; kind sets direction."
        )
    if (line_sum := sum(line.amount for line in e.lines)) != e.subtotal:
        yield Finding(
            "subtotal_mismatch", "error", f"Lines add to {line_sum}, subtotal says {e.subtotal}."
        )
    if (tax_sum := sum(line.tax_amount for line in e.lines)) != e.tax_total:
        yield Finding(
            "tax_mismatch", "error", f"Line tax adds to {tax_sum}, tax says {e.tax_total}."
        )
    if e.subtotal + e.tax_total != e.total:
        yield Finding(
            "total_mismatch",
            "error",
            f"Subtotal {e.subtotal} + tax {e.tax_total} = {e.subtotal + e.tax_total}, "
            f"total says {e.total}.",
        )


def _references(e: ProposedEntry, ctx: ValidationContext):
    for line in e.lines:
        account = ctx.accounts.get(line.account_id)
        if account is None:
            yield Finding("unknown_account", "error", f"Account {line.account_id} is not in QBO.")
        elif not account.active:
            yield Finding("inactive_account", "error", f"Account {account.name} is inactive.")
        code = ctx.tax_codes.get(line.tax_code_id)
        if code is None:
            yield Finding(
                "unknown_tax_code", "error", f"Tax code {line.tax_code_id} is not in QBO."
            )
        elif not code.active:
            yield Finding("inactive_tax_code", "error", f"Tax code {code.name} is inactive.")


def _tax(e: ProposedEntry, ctx: ValidationContext):
    expected = Decimal(0)
    for line in e.lines:
        code = ctx.tax_codes.get(line.tax_code_id)
        if code is None:
            return  # reported by _references
        expected += line.amount * code.purchase_rate
    # Suppliers round per line or per invoice; allow a cent per line either way.
    tolerance = CENT * len(e.lines)
    if abs(expected.quantize(CENT) - e.tax_total) > tolerance:
        yield Finding(
            "tax_rate_mismatch",
            "hold",
            f"Tax of {e.tax_total} does not match the tax codes chosen "
            f"(expected about {expected.quantize(CENT)}).",
        )
    claims_itc = e.tax_total > 0 and e.currency == ctx.home_currency
    if claims_itc and e.total >= ITC_NUMBER_THRESHOLD:
        number = (e.supplier_tax_number or "").strip()
        if not number:
            yield Finding(
                "missing_tax_number",
                "flag",
                "No GST/HST registration number on the invoice; the input tax credit may be denied.",
            )
        elif not GST_NUMBER.match(number):
            yield Finding(
                "invalid_tax_number",
                "flag",
                f"'{number}' does not look like a GST/HST number (123456789 RT 0001).",
            )


def _duplicates(e: ProposedEntry, ctx: ValidationContext):
    if e.document_sha256 and e.document_sha256 in ctx.posted_hashes:
        yield Finding("duplicate_document", "error", "This exact document was already posted.")
    if e.invoice_number:
        if invoice_key(e.vendor_name, e.invoice_number) in ctx.existing_invoices:
            yield Finding(
                "duplicate_invoice",
                "error",
                f"{e.vendor_name} invoice {e.invoice_number} is already in the books.",
            )
    elif e.kind == "bill":
        yield Finding(
            "missing_invoice_number",
            "flag",
            "No invoice number, so duplicates are harder to catch.",
        )


def _dates(e: ProposedEntry, ctx: ValidationContext):
    if ctx.closing_date and e.invoice_date <= ctx.closing_date:
        yield Finding(
            "closed_period",
            "hold",
            f"Dated {e.invoice_date}, on or before the closing date {ctx.closing_date}.",
        )
    if e.invoice_date > ctx.today + timedelta(days=7):
        yield Finding("future_date", "hold", f"Dated {e.invoice_date}, in the future.")
    elif e.invoice_date < ctx.today - timedelta(days=365):
        yield Finding("old_date", "flag", f"Dated {e.invoice_date}, over a year ago.")
    if e.due_date and e.due_date < e.invoice_date:
        yield Finding("due_before_invoice", "flag", "Due date is before the invoice date.")


def _amount_and_currency(e: ProposedEntry, ctx: ValidationContext):
    if e.currency != ctx.home_currency:
        # The materiality check below compares the foreign amount as if it were CAD. That is
        # close enough for USD at this threshold; revisit when exchange rates are handled.
        yield Finding(
            "foreign_currency", "flag", f"Invoiced in {e.currency}; check the exchange rate used."
        )
    if e.total >= ctx.materiality:
        yield Finding(
            "material_amount",
            "hold",
            f"{e.currency} {e.total} is at or above the {ctx.materiality} threshold.",
        )


def _vendor(e: ProposedEntry, ctx: ValidationContext):
    if e.vendor_id is None:
        yield Finding("new_vendor", "flag", f"{e.vendor_name} is a new vendor.")


def _tags(e: ProposedEntry, ctx: ValidationContext):
    for tag in sorted(e.tags):
        if tag in HOLD_TAGS:
            yield Finding(f"tag_{tag}", "hold", f"Tagged {tag.replace('_', ' ')}.")
        elif tag in FLAG_TAGS:
            yield Finding(f"tag_{tag}", "flag", f"Tagged {tag.replace('_', ' ')}.")
        elif tag not in ALL_TAGS:
            yield Finding("unknown_tag", "hold", f"Unrecognised tag '{tag}'.")


def _prepaid(e: ProposedEntry, ctx: ValidationContext):
    if not (e.service_start and e.service_end):
        return
    if (e.service_end - e.service_start).days < PREPAID_REVIEW_DAYS:
        return
    expensed = [
        line
        for line in e.lines
        if (a := ctx.accounts.get(line.account_id)) and a.account_type in EXPENSE_ACCOUNT_TYPES
    ]
    if expensed:
        yield Finding(
            "possible_prepaid",
            "flag",
            f"Covers {e.service_start} to {e.service_end} but is expensed; consider a prepaid.",
        )
