"""Copy QuickBooks reference data into Bob's cache: chart of accounts, vendors, tax codes and
company settings (closing date, home currency). Read-only against QBO."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from bob import audit
from bob.db import utcnow
from bob.models import QboAccount, QboBill, QboSetting, QboTaxCode, QboVendor
from bob.qbo.client import QBOClient

log = logging.getLogger(__name__)

BILL_HISTORY_DAYS = 540


@dataclass(frozen=True)
class SyncSummary:
    accounts: int
    vendors: int
    tax_codes: int
    bills: int
    closing_date: str | None


def _purchase_rate(tax_code: dict, rates: dict[str, Decimal]) -> Decimal:
    details = (tax_code.get("PurchaseTaxRateList") or {}).get("TaxRateDetail") or []
    percent = sum((rates.get(d["TaxRateRef"]["value"], Decimal(0)) for d in details), Decimal(0))
    return (percent / 100).quantize(Decimal("0.0001"))


def _expense_lines(row: dict) -> list[dict]:
    out = []
    for line in row.get("Line") or []:
        detail = line.get("AccountBasedExpenseLineDetail")
        if not detail:
            continue
        out.append(
            {
                "account_id": (detail.get("AccountRef") or {}).get("value"),
                "account_name": (detail.get("AccountRef") or {}).get("name"),
                "amount": str(line.get("Amount")),
                "tax_code_id": (detail.get("TaxCodeRef") or {}).get("value"),
                "description": line.get("Description") or "",
            }
        )
    return out


def _set(session: Session, key: str, value: str | None) -> None:
    session.merge(QboSetting(key=key, value=value, synced_at=utcnow()))


def sync_reference_data(session: Session, client: QBOClient) -> SyncSummary:
    now = utcnow()

    accounts = client.query("select * from Account")
    for a in accounts:
        session.merge(
            QboAccount(
                id=a["Id"],
                name=a["Name"],
                fully_qualified_name=a.get("FullyQualifiedName") or a["Name"],
                account_type=a["AccountType"],
                account_sub_type=a.get("AccountSubType"),
                acct_num=a.get("AcctNum"),
                active=a.get("Active", True),
                synced_at=now,
            )
        )

    vendors = client.query("select * from Vendor")
    for v in vendors:
        session.merge(
            QboVendor(
                id=v["Id"],
                display_name=v["DisplayName"],
                email=(v.get("PrimaryEmailAddr") or {}).get("Address"),
                active=v.get("Active", True),
                synced_at=now,
            )
        )

    rates = {
        r["Id"]: Decimal(str(r.get("RateValue", 0))) for r in client.query("select * from TaxRate")
    }
    tax_codes = client.query("select * from TaxCode")
    for t in tax_codes:
        session.merge(
            QboTaxCode(
                id=t["Id"],
                name=t["Name"],
                purchase_rate=_purchase_rate(t, rates),
                active=t.get("Active", True),
                synced_at=now,
            )
        )

    since = (now - timedelta(days=BILL_HISTORY_DAYS)).date().isoformat()
    bills = 0
    for entity in ("Bill", "VendorCredit", "Purchase"):
        for row in client.query(f"select * from {entity} where TxnDate >= '{since}'"):
            vendor = row.get("VendorRef") or row.get("EntityRef") or {}
            session.merge(
                QboBill(
                    id=f"{entity}:{row['Id']}",
                    entity=entity,
                    vendor_id=vendor.get("value"),
                    vendor_name=vendor.get("name"),
                    doc_number=row.get("DocNumber"),
                    txn_date=row["TxnDate"],
                    total=Decimal(str(row.get("TotalAmt", 0))),
                    lines=_expense_lines(row),
                    synced_at=now,
                )
            )
            bills += 1

    prefs = client.preferences()
    closing = (prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")
    currency = ((prefs.get("CurrencyPrefs") or {}).get("HomeCurrency") or {}).get("value")
    _set(session, "closing_date", closing)
    _set(session, "home_currency", currency or "CAD")
    _set(session, "last_sync", now.isoformat())

    summary = SyncSummary(len(accounts), len(vendors), len(tax_codes), bills, closing)
    audit.record(session, "qbo.synced", "qbo", None, summary.__dict__)
    return summary
