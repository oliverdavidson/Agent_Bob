"""A fictional QuickBooks setup for BridgeWerk used by the coding eval: chart of accounts,
tax codes, vendors and a little bill history. Replace with a sandbox sync once connected."""

from decimal import Decimal

from bob.models import QboAccount, QboBill, QboSetting, QboTaxCode, QboVendor

ACCOUNTS = [
    ("10", "Cash - Operating", "Bank"),
    ("12", "Accounts Receivable (A/R)", "Accounts Receivable"),
    ("13", "Prepaid Expenses", "Other Current Asset"),
    ("15", "Furniture and Equipment", "Fixed Asset"),
    ("21", "Accounts Payable (A/P)", "Accounts Payable"),
    ("22", "Company Visa", "Credit Card"),
    ("23", "GST/HST Payable", "Other Current Liability"),
    ("31", "Opening Balance Equity", "Equity"),
    ("40", "Management Fee Revenue", "Income"),
    ("60", "Software and Subscriptions", "Expense"),
    ("61", "Legal Fees", "Expense"),
    ("62", "Professional Fees - Advisory and Tax", "Expense"),
    ("63", "Rent and Occupancy", "Expense"),
    ("64", "Insurance Expense", "Expense"),
    ("65", "Deal Costs", "Expense"),
    ("66", "Office Supplies", "Expense"),
    ("67", "Travel and Parking", "Expense"),
    ("68", "Computer Equipment Expense", "Expense"),
    ("69", "Market Data and Research", "Expense"),
]
TAX_CODES = [
    ("4", "GST", "0.05"),
    ("8", "HST ON", "0.13"),
    ("2", "Exempt", "0"),
    ("9", "Out of Scope", "0"),
    ("10", "Zero-rated", "0"),
]
VENDORS = [
    ("17", "Northwind Cloud Software Inc."),
    ("18", "Harrow & Pike LLP"),
    ("19", "Bow River Office Supply Ltd."),
    ("20", "Chinook Properties Inc."),
    ("21", "Prairie Mutual Insurance Company"),
    ("22", "Lakeshore Advisory Group Inc."),
    ("23", "Summit Analytics LLC"),
]
HISTORY = [
    # (vendor id, vendor name, doc number, date, total, account, tax code, amount)
    (
        "17",
        "Northwind Cloud Software Inc.",
        "NW-10301",
        "2026-08-01",
        "1260.00",
        "60",
        "4",
        "1200.00",
    ),
    (
        "17",
        "Northwind Cloud Software Inc.",
        "NW-10344",
        "2026-09-01",
        "1260.00",
        "60",
        "4",
        "1200.00",
    ),
    ("18", "Harrow & Pike LLP", "HP-2204", "2026-08-12", "3150.00", "61", "4", "3000.00"),
    ("19", "Bow River Office Supply Ltd.", "55120", "2026-08-14", "412.65", "66", "4", "393.00"),
    (
        "20",
        "Chinook Properties Inc.",
        "R-2026-10-1400",
        "2026-09-20",
        "11518.50",
        "63",
        "4",
        "10970.00",
    ),
    ("23", "Summit Analytics LLC", "INV-3350", "2026-09-01", "2250.00", "69", "9", "2250.00"),
]


def seed(session) -> None:
    for aid, name, kind in ACCOUNTS:
        session.add(QboAccount(id=aid, name=name, fully_qualified_name=name, account_type=kind))
    for tid, name, rate in TAX_CODES:
        session.add(QboTaxCode(id=tid, name=name, purchase_rate=Decimal(rate)))
    for vid, name in VENDORS:
        session.add(QboVendor(id=vid, display_name=name))
    for i, (vid, vname, num, when, total, account, tax, amount) in enumerate(HISTORY):
        session.add(
            QboBill(
                id=f"Bill:h{i}",
                entity="Bill",
                vendor_id=vid,
                vendor_name=vname,
                doc_number=num,
                txn_date=when,
                total=Decimal(total),
                lines=[{"account_id": account, "amount": amount, "tax_code_id": tax}],
            )
        )
    session.add(QboSetting(key="closing_date", value="2026-09-30"))
    session.add(QboSetting(key="home_currency", value="CAD"))
