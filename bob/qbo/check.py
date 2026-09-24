"""Check the QuickBooks connection before Bob relies on it.

    python -m bob.qbo.check                 # read-only: company, tax set-up, reference sync
    python -m bob.qbo.check --write-test    # sandbox only: create, attach and delete test entries

The read-only check confirms the company is Canadian, has a GST purchase tax code and expense
accounts, and runs the same reference sync the worker runs.

The write test posts a bill, a vendor credit and (when there is a payment account) a card
purchase through Bob's own payload builder, reads each back, and deletes it the way undo
does. The bill's invoice tax (3.33) differs from the per-line tax QuickBooks would compute
(1.67 + 1.67), so the test shows whether QuickBooks keeps the invoice's tax or recomputes it.
It runs only against a sandbox company and turns writes on for itself alone.
"""

import argparse
import secrets
import sys
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit
from bob.accounting.entry import ProposedEntry, ProposedLine
from bob.accounting.posting import ENTITY_FOR_KIND, build_payload
from bob.config import Settings, get_settings
from bob.db import as_utc, make_engine, make_sessionmaker, utcnow
from bob.models import Proposal, QboAccount, QboConnection, QboSetting, QboTaxCode, QboVendor
from bob.qbo.client import NotConnected, QBOClient, QBOError
from bob.qbo.oauth import OAuthError
from bob.qbo.sync import sync_reference_data

GST = Decimal("0.05")
CENT = Decimal("0.01")
CHECK_VENDOR = "Bob connection check"
PAYMENT_ACCOUNT_TYPES = {"Credit Card", "Bank", "Other Current Liability"}
REFRESH_WARNING = timedelta(days=14)
# The smallest well-formed PDF QuickBooks accepts as an attachment.
TEST_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@dataclass(frozen=True)
class Check:
    status: Literal["ok", "warn", "fail"]
    message: str


def _setting(session: Session, key: str) -> str | None:
    row = session.get(QboSetting, key)
    return row.value if row else None


def _gst_code(session: Session) -> QboTaxCode | None:
    codes = session.scalars(select(QboTaxCode).where(QboTaxCode.active.is_(True))).all()
    return next((c for c in codes if Decimal(c.purchase_rate) == GST), None)


def _accounts(session: Session, types: set[str]) -> list[QboAccount]:
    return session.scalars(
        select(QboAccount)
        .where(QboAccount.active.is_(True), QboAccount.account_type.in_(types))
        .order_by(QboAccount.name)
    ).all()


def read_checks(session: Session, client: QBOClient) -> list[Check]:
    settings = client.settings
    out: list[Check] = []
    conn = session.scalars(
        select(QboConnection).where(QboConnection.environment == settings.qbo_environment)
    ).first()
    if conn is None:
        raise NotConnected("No QuickBooks connection. Run: python -m bob.qbo.connect")
    left = as_utc(conn.refresh_expires_at) - utcnow()
    out.append(
        Check(
            "warn" if left < REFRESH_WARNING else "ok",
            f"Connected to company {conn.realm_id} ({settings.qbo_environment}); "
            f"the connection lapses in {left.days} days unless Bob syncs before then",
        )
    )

    info = client.read("CompanyInfo", conn.realm_id)
    country = info.get("Country")
    out.append(
        Check("ok", f"Company is {info.get('CompanyName')}, in Canada")
        if country == "CA"
        else Check(
            "fail",
            f"Company {info.get('CompanyName')} is set up for {country or 'an unknown country'}. "
            "Bob's GST handling needs a Canadian company; create a Canadian sandbox company.",
        )
    )

    summary = sync_reference_data(session, client)
    out.append(
        Check(
            "ok",
            f"Synced {summary.accounts} accounts, {summary.vendors} vendors, "
            f"{summary.tax_codes} tax codes and {summary.bills} recent transactions",
        )
    )

    currency = _setting(session, "home_currency")
    out.append(
        Check("ok", "Home currency is CAD")
        if currency == "CAD"
        else Check("fail", f"Home currency is {currency}, not CAD")
    )
    out.append(
        Check("ok", f"Closing date is {summary.closing_date}")
        if summary.closing_date
        else Check(
            "warn",
            "No closing date is set, so nothing stops a post into a finished period. "
            "Set one in QuickBooks after each close.",
        )
    )

    gst = _gst_code(session)
    out.append(
        Check("ok", f"GST purchase tax code: {gst.name} (id {gst.id})")
        if gst
        else Check("fail", "No active tax code with a 5% purchase rate (GST)")
    )

    expense = _accounts(session, {"Expense", "Other Expense", "Cost of Goods Sold"})
    out.append(
        Check("ok", f"{len(expense)} active expense accounts")
        if expense
        else Check("fail", "No active expense accounts")
    )

    pay_id = settings.expense_payment_account_id
    if not pay_id:
        out.append(
            Check(
                "warn",
                "BOB_EXPENSE_PAYMENT_ACCOUNT_ID is not set, so receipts will be held. Candidates: "
                + (
                    ", ".join(
                        f"{a.name} (id {a.id})" for a in _accounts(session, PAYMENT_ACCOUNT_TYPES)
                    )
                    or "none"
                ),
            )
        )
    else:
        account = session.get(QboAccount, pay_id)
        out.append(
            Check("ok", f"Receipts are paid from {account.name} ({account.account_type})")
            if account and account.active and account.account_type in PAYMENT_ACCOUNT_TYPES
            else Check(
                "fail",
                f"BOB_EXPENSE_PAYMENT_ACCOUNT_ID={pay_id} is not an active card, bank or "
                "clearing account in this company",
            )
        )
    return out


def _check_vendor(session: Session, client: QBOClient, run: str) -> str:
    """QuickBooks vendors cannot be deleted, so one test vendor is created once and reused."""
    vendor = session.scalars(
        select(QboVendor).where(QboVendor.display_name == CHECK_VENDOR)
    ).first()
    if vendor:
        return vendor.id
    created = client.create("Vendor", {"DisplayName": CHECK_VENDOR}, request_id=f"{run}-vendor")
    session.merge(QboVendor(id=created["Id"], display_name=created["DisplayName"]))
    return created["Id"]


def _entry(kind, vendor_id, account_id, tax_code_id, amounts, tax_total) -> ProposedEntry:
    subtotal = sum(amounts, Decimal(0))
    return ProposedEntry(
        kind=kind,
        vendor_name=CHECK_VENDOR,
        vendor_id=vendor_id,
        invoice_number=None,
        invoice_date=utcnow().date(),
        currency="CAD",
        subtotal=subtotal,
        tax_total=tax_total,
        total=subtotal + tax_total,
        lines=tuple(
            ProposedLine(account_id, a, tax_code_id, (a * GST).quantize(CENT), "Test")
            for a in amounts
        ),
    )


def _round_trip(
    session: Session, client: QBOClient, settings: Settings, entry: ProposedEntry, run: str
) -> list[Check]:
    entity = ENTITY_FOR_KIND[entry.kind]
    request_id = f"{run}-{entry.kind}"
    payload = build_payload(entry, Proposal(id=0, version=1), settings, "CAD")
    payload["PrivateNote"] = f"Bob connection check {run}; safe to delete"
    payload["DocNumber"] = f"CHK-{run[-8:]}-{entry.kind[:2].upper()}"

    out: list[Check] = []
    try:
        created = client.create(entity, payload, request_id=request_id)
    except QBOError as err:
        return [Check("fail", f"{entity}: create failed: {err}")]
    try:
        stored = client.read(entity, created["Id"])
        tax = Decimal(str((stored.get("TxnTaxDetail") or {}).get("TotalTax", 0))).quantize(CENT)
        total = Decimal(str(stored.get("TotalAmt", 0))).quantize(CENT)
        if (tax, total) == (entry.tax_total, entry.total):
            out.append(Check("ok", f"{entity}: created; QuickBooks kept tax {tax}, total {total}"))
        else:
            out.append(
                Check(
                    "fail",
                    f"{entity}: sent tax {entry.tax_total} and total {entry.total}; QuickBooks "
                    f"stored tax {tax} and total {total}. Posting must send explicit tax lines "
                    "so the books match the invoice.",
                )
            )
        if entity == "Bill":
            client.attach(
                entity,
                created["Id"],
                "bob-check.pdf",
                "application/pdf",
                TEST_PDF,
                request_id=f"{request_id}-attach",
            )
            out.append(Check("ok", f"{entity}: PDF attached"))
    except QBOError as err:
        out.append(Check("fail", f"{entity}: {err}"))
    finally:
        # Undo's path: read the current SyncToken, then delete.
        try:
            current = client.read(entity, created["Id"])
            client.delete(
                entity, created["Id"], current["SyncToken"], request_id=f"{request_id}-delete"
            )
            out.append(Check("ok", f"{entity}: deleted again (the undo path works)"))
        except QBOError as err:
            out.append(
                Check("fail", f"{entity} {created['Id']}: delete failed, remove it by hand: {err}")
            )
    return out


def write_test(session: Session, client: QBOClient) -> list[Check]:
    settings = client.settings
    if settings.qbo_environment != "sandbox":
        raise ValueError("The write test only runs against a sandbox company.")
    gst = _gst_code(session)
    expense = _accounts(session, {"Expense"})
    if not (gst and expense):
        return [Check("fail", "Write test skipped: needs a GST tax code and an expense account")]
    run = f"bob-check-{secrets.token_hex(4)}"
    vendor_id = _check_vendor(session, client, run)
    account = expense[0].id

    cases = [
        # Two lines of 33.33: GST on the subtotal is 3.33, per line it is 1.67 + 1.67 = 3.34.
        _entry("bill", vendor_id, account, gst.id, [Decimal("33.33")] * 2, Decimal("3.33")),
        _entry("credit", vendor_id, account, gst.id, [Decimal("100.00")], Decimal("5.00")),
    ]
    pay_id = settings.expense_payment_account_id or next(
        (a.id for a in _accounts(session, {"Credit Card"})), None
    )
    out: list[Check] = []
    if pay_id:
        settings = settings.model_copy(update={"expense_payment_account_id": pay_id})
        cases.append(
            _entry("expense", vendor_id, account, gst.id, [Decimal("50.00")], Decimal("2.50"))
        )
    else:
        out.append(Check("warn", "Card purchase skipped: no payment account to post it against"))

    for entry in cases:
        out.extend(_round_trip(session, client, settings, entry, run))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bob.qbo.check")
    parser.add_argument(
        "--write-test",
        action="store_true",
        help="sandbox only: create, read back, attach to and delete test transactions",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not (settings.qbo_client_id and settings.qbo_client_secret):
        print("Set BOB_QBO_CLIENT_ID and BOB_QBO_CLIENT_SECRET first.")
        return 1
    if args.write_test and settings.qbo_environment != "sandbox":
        print("The write test only runs against a sandbox company (BOB_QBO_ENVIRONMENT=sandbox).")
        return 1

    factory = make_sessionmaker(make_engine(settings.database_url))
    checks: list[Check] = []
    try:
        with factory() as session:
            checks = read_checks(session, QBOClient(factory, settings))
            if args.write_test:
                writer = QBOClient(
                    factory, settings.model_copy(update={"qbo_writes_enabled": True})
                )
                checks += write_test(session, writer)
            failures = [c.message for c in checks if c.status == "fail"]
            audit.record(
                session,
                "qbo.checked",
                "qbo",
                None,
                {"write_test": args.write_test, "failures": failures},
                actor="cli",
            )
            session.commit()
    except (NotConnected, OAuthError, QBOError) as err:
        checks.append(Check("fail", str(err)))

    for c in checks:
        print(f"  {c.status.upper():5} {c.message}")
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
