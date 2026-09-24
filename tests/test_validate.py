from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from bob.accounting.entry import ProposedEntry, ProposedLine
from bob.accounting.validate import (
    AccountInfo,
    TaxCodeInfo,
    ValidationContext,
    invoice_key,
    validate,
)

CTX = ValidationContext(
    accounts={
        "60": AccountInfo("60", "Software Subscriptions", "Expense"),
        "61": AccountInfo("61", "Legal Fees", "Expense"),
        "13": AccountInfo("13", "Prepaid Expenses", "Other Current Asset"),
        "99": AccountInfo("99", "Old Account", "Expense", active=False),
    },
    tax_codes={
        "GST": TaxCodeInfo("GST", "GST", D("0.05")),
        "HSTON": TaxCodeInfo("HSTON", "HST ON", D("0.13")),
        "EX": TaxCodeInfo("EX", "Exempt", D(0)),
    },
    closing_date=date(2026, 9, 30),
    today=date(2026, 10, 15),
    materiality=D(25000),
    existing_invoices=frozenset({invoice_key("Harrow & Pike LLP", "INV-0042")}),
    posted_hashes=frozenset({"abc123"}),
)


def entry(**overrides) -> ProposedEntry:
    base = ProposedEntry(
        kind="bill",
        vendor_name="Northwind Software Inc.",
        vendor_id="17",
        invoice_number="NW-1001",
        invoice_date=date(2026, 10, 3),
        currency="CAD",
        subtotal=D("1200.00"),
        tax_total=D("60.00"),
        total=D("1260.00"),
        lines=(ProposedLine("60", D("1200.00"), "GST", D("60.00"), "Licences"),),
        supplier_tax_number="123456789 RT 0001",
    )
    return replace(base, **overrides)


def test_clean_invoice_posts():
    result = validate(entry(), CTX)
    assert result.findings == []
    assert result.outcome == "post"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"total": D("1261.00")}, "total_mismatch"),
        ({"subtotal": D("1100.00"), "total": D("1160.00")}, "subtotal_mismatch"),
        ({"tax_total": D("61.00"), "total": D("1261.00")}, "tax_mismatch"),
        ({"total": D("1260.005")}, "fractional_cents"),
        ({"lines": ()}, "no_lines"),
        ({"lines": (ProposedLine("404", D("1200.00"), "GST", D("60.00")),)}, "unknown_account"),
        ({"lines": (ProposedLine("99", D("1200.00"), "GST", D("60.00")),)}, "inactive_account"),
        ({"lines": (ProposedLine("60", D("1200.00"), "PST", D("60.00")),)}, "unknown_tax_code"),
        ({"document_sha256": "abc123"}, "duplicate_document"),
    ],
)
def test_errors_reject(overrides, code):
    result = validate(entry(**overrides), CTX)
    assert code in result.codes()
    assert result.outcome == "reject"


@pytest.mark.parametrize(
    ("vendor", "number"),
    [("Harrow & Pike", "INV-00042"), ("HARROW AND PIKE, LLP", "inv 42")],
)
def test_duplicate_invoice_matches_despite_formatting(vendor, number):
    result = validate(entry(vendor_name=vendor, invoice_number=number), CTX)
    assert "duplicate_invoice" in result.codes()
    assert result.outcome == "reject"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"invoice_date": date(2026, 9, 30)}, "closed_period"),
        ({"invoice_date": date(2026, 11, 30)}, "future_date"),
        (
            {
                "subtotal": D("30000.00"),
                "tax_total": D("1500.00"),
                "total": D("31500.00"),
                "lines": (ProposedLine("61", D("30000.00"), "GST", D("1500.00")),),
            },
            "material_amount",
        ),
        ({"tags": frozenset({"acquisition_related"})}, "tag_acquisition_related"),
        ({"tags": frozenset({"bank_details_changed"})}, "tag_bank_details_changed"),
        ({"tags": frozenset({"made_up"})}, "unknown_tag"),
        # Charged 13% but coded GST: the tax does not match the code.
        (
            {
                "tax_total": D("156.00"),
                "total": D("1356.00"),
                "lines": (ProposedLine("60", D("1200.00"), "GST", D("156.00")),),
            },
            "tax_rate_mismatch",
        ),
    ],
)
def test_holds(overrides, code):
    result = validate(entry(**overrides), CTX)
    assert code in result.codes()
    assert result.outcome == "hold"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"vendor_id": None}, "new_vendor"),
        ({"supplier_tax_number": None}, "missing_tax_number"),
        ({"supplier_tax_number": "GST-555"}, "invalid_tax_number"),
        (
            {
                "currency": "USD",
                "tax_total": D(0),
                "total": D("1200.00"),
                "lines": (ProposedLine("60", D("1200.00"), "EX", D(0)),),
            },
            "foreign_currency",
        ),
        ({"invoice_number": None}, "missing_invoice_number"),
        (
            {"service_start": date(2026, 10, 1), "service_end": date(2027, 9, 30)},
            "possible_prepaid",
        ),
        ({"tags": frozenset({"capex"})}, "tag_capex"),
    ],
)
def test_flags_still_post(overrides, code):
    result = validate(entry(**overrides), CTX)
    assert code in result.codes()
    assert result.outcome == "post_and_flag"


def test_old_invoice_in_closed_period_is_held():
    result = validate(entry(invoice_date=date(2025, 6, 1)), CTX)
    assert {"old_date", "closed_period"} <= set(result.codes())
    assert result.outcome == "hold"


def test_small_receipt_needs_no_tax_number():
    small = entry(
        kind="expense",
        subtotal=D("40.00"),
        tax_total=D("2.00"),
        total=D("42.00"),
        lines=(ProposedLine("60", D("40.00"), "GST", D("2.00")),),
        supplier_tax_number=None,
    )
    assert validate(small, CTX).outcome == "post"


def test_prepaid_account_is_not_flagged():
    annual = entry(
        service_start=date(2026, 10, 1),
        service_end=date(2027, 9, 30),
        lines=(ProposedLine("13", D("1200.00"), "GST", D("60.00")),),
    )
    assert "possible_prepaid" not in validate(annual, CTX).codes()


def test_per_line_rounding_is_tolerated():
    # Three lines of 33.33 at 5% round to 1.67 each (5.01) vs 5.00 on the subtotal.
    lines = tuple(ProposedLine("60", D("33.33"), "GST", D("1.67")) for _ in range(3))
    e = entry(subtotal=D("99.99"), tax_total=D("5.01"), total=D("105.00"), lines=lines)
    assert "tax_rate_mismatch" not in validate(e, CTX).codes()
