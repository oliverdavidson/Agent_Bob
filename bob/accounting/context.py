"""Build the validator's context from the QuickBooks cache."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bob.accounting.validate import AccountInfo, TaxCodeInfo, ValidationContext
from bob.models import QboAccount, QboSetting, QboTaxCode


def build_context(
    session: Session,
    today: date,
    materiality: Decimal,
    existing_invoices: frozenset[tuple[str, str]] = frozenset(),
    posted_hashes: frozenset[str] = frozenset(),
) -> ValidationContext:
    settings = {s.key: s.value for s in session.scalars(select(QboSetting))}
    closing = settings.get("closing_date")
    return ValidationContext(
        accounts={
            a.id: AccountInfo(a.id, a.fully_qualified_name, a.account_type, a.active)
            for a in session.scalars(select(QboAccount))
        },
        tax_codes={
            t.id: TaxCodeInfo(t.id, t.name, Decimal(t.purchase_rate), t.active)
            for t in session.scalars(select(QboTaxCode))
        },
        closing_date=date.fromisoformat(closing) if closing else None,
        today=today,
        materiality=materiality,
        home_currency=settings.get("home_currency") or "CAD",
        existing_invoices=existing_invoices,
        posted_hashes=posted_hashes,
    )
