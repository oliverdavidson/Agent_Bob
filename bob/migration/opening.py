"""Opening balances from a Business Central trial balance.

    python -m bob.migration.opening suggest --tb tb.csv --out mapping.csv
    python -m bob.migration.opening prepare --tb tb.csv --mapping mapping.csv --date 2026-09-30
    python -m bob.migration.opening post    --tb tb.csv --mapping mapping.csv --date 2026-09-30 \\
                                             --confirm <fingerprint>
    python -m bob.migration.opening prepare ... --previous opening-2026-09-30.json   # true-up

How it books:
- One journal entry dated the cutover date, one line per QuickBooks account.
- Accounts receivable and payable are left out: QuickBooks needs them as individual open
  invoices and bills (a later import). Their net goes to Opening Balance Equity for now, and
  loading the open items brings Opening Balance Equity back to zero.
- `post` only runs with the fingerprint printed by `prepare`, so exactly what was reviewed is
  what gets posted. The fingerprint is also the QuickBooks request id, so a repeat is harmless.
- With --previous, the entry is the difference from an earlier opening entry (the true-up once
  September is final in Business Central).
"""

import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit
from bob.config import get_settings
from bob.db import make_engine, make_sessionmaker
from bob.models import QboAccount, QboSetting

AR_AP_TYPES = {"Accounts Receivable", "Accounts Payable"}
OBE_NAME = "Opening Balance Equity"
CENT = Decimal("0.01")

NUMBER_COLUMNS = (
    "no.",
    "no",
    "account",
    "account no.",
    "account number",
    "g/l account no.",
    "number",
    "acct",
)
NAME_COLUMNS = ("name", "account name", "g/l account name", "description")
DEBIT_COLUMNS = ("debit", "debit amount", "dr")
CREDIT_COLUMNS = ("credit", "credit amount", "cr")
BALANCE_COLUMNS = ("balance", "net change", "balance at date", "amount")


class OpeningError(ValueError):
    pass


@dataclass(frozen=True)
class TbRow:
    number: str
    name: str
    balance: Decimal  # debit positive, credit negative


@dataclass(frozen=True)
class JournalLine:
    account_id: str
    account_name: str
    amount: Decimal  # debit positive, credit negative
    description: str


@dataclass
class Prepared:
    txn_date: date
    lines: list[JournalLine]
    receivable: Decimal
    payable: Decimal
    obe_account_id: str
    warnings: list[str]
    true_up: bool = False

    def payload(self) -> dict:
        kind = "TRUEUP" if self.true_up else "OPENING"
        return {
            "TxnDate": self.txn_date.isoformat(),
            "DocNumber": f"{kind}-{self.txn_date:%Y%m%d}",
            "PrivateNote": (
                f"{'True-up of opening' if self.true_up else 'Opening'} balances from the Business "
                f"Central trial balance at {self.txn_date}. Prepared by Bob; signed off before posting."
            ),
            "Line": [
                {
                    "DetailType": "JournalEntryLineDetail",
                    "Amount": float(abs(line.amount)),
                    "Description": line.description[:4000],
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit" if line.amount > 0 else "Credit",
                        "AccountRef": {"value": line.account_id},
                    },
                }
                for line in self.lines
                if line.amount != 0
            ],
        }

    def fingerprint(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def report(self) -> str:
        debits = sum((ln.amount for ln in self.lines if ln.amount > 0), Decimal(0))
        credits = -sum((ln.amount for ln in self.lines if ln.amount < 0), Decimal(0))
        out = [
            f"{'True-up' if self.true_up else 'Opening'} journal entry dated {self.txn_date}",
            f"{'Account':50} {'Debit':>15} {'Credit':>15}",
        ]
        for ln in self.lines:
            if ln.amount == 0:
                continue
            dr = f"{ln.amount:,.2f}" if ln.amount > 0 else ""
            cr = f"{-ln.amount:,.2f}" if ln.amount < 0 else ""
            out.append(f"{ln.account_name[:50]:50} {dr:>15} {cr:>15}")
        out.append(f"{'Total':50} {debits:>15,.2f} {credits:>15,.2f}")
        out.append("")
        out.append(
            f"Left out, to load as open items: receivables {self.receivable:,.2f}, payables {-self.payable:,.2f}"
        )
        out.append("Opening Balance Equity returns to zero once those open items are loaded.")
        out.extend(f"WARNING: {w}" for w in self.warnings)
        out.append(f"\nFingerprint: {self.fingerprint()}")
        return "\n".join(out)


# --- reading --------------------------------------------------------------------------------


def parse_amount(text: str | None) -> Decimal:
    text = (text or "").strip().replace(",", "").replace("$", "")
    if not text or text in {"-", "—"}:
        return Decimal(0)
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.endswith("-"):  # 1234.56- style credits
        negative, text = True, text[:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise OpeningError(f"Not an amount: {text!r}") from None
    return -value if negative else value


def _find(header: list[str], options: tuple[str, ...]) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for option in options:
        if option in lowered:
            return lowered.index(option)
    return None


def read_trial_balance(path: Path) -> list[TbRow]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    if not rows:
        raise OpeningError("The trial balance file is empty.")
    header, body = rows[0], rows[1:]
    number = _find(header, NUMBER_COLUMNS)
    name = _find(header, NAME_COLUMNS)
    debit, credit = _find(header, DEBIT_COLUMNS), _find(header, CREDIT_COLUMNS)
    balance = _find(header, BALANCE_COLUMNS)
    if number is None or (balance is None and (debit is None or credit is None)):
        raise OpeningError(
            f"Could not find account number and debit/credit (or balance) columns in {header}."
        )
    out = []
    for r in body:
        acct = r[number].strip() if number < len(r) else ""
        label = r[name].strip() if name is not None and name < len(r) else ""
        if not acct or re.search(r"\btotal\b", f"{acct} {label}", re.IGNORECASE):
            continue  # headings, blank lines and total rows
        if debit is not None and credit is not None:
            value = parse_amount(r[debit] if debit < len(r) else "") - parse_amount(
                r[credit] if credit < len(r) else ""
            )
        else:
            value = parse_amount(r[balance])
        out.append(TbRow(acct, label, value))
    return out


def read_mapping(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        mapping = {}
        for r in reader:
            bc = (r.get("bc_account") or "").strip()
            qbo = (r.get("qbo_account_id") or "").strip()
            if bc and qbo:
                mapping[bc] = qbo
        return mapping


# --- preparing ------------------------------------------------------------------------------


def suggest_mapping(session: Session, tb: list[TbRow]) -> list[dict]:
    """Draft mapping by closest account name. A person reviews every row before use."""
    accounts = session.scalars(select(QboAccount).where(QboAccount.active.is_(True))).all()
    by_name = {a.fully_qualified_name.lower(): a for a in accounts}
    rows = []
    for row in tb:
        match = difflib.get_close_matches(row.name.lower(), list(by_name), n=1, cutoff=0.6)
        account = by_name[match[0]] if match else None
        rows.append(
            {
                "bc_account": row.number,
                "bc_name": row.name,
                "qbo_account_id": account.id if account else "",
                "qbo_name": account.fully_qualified_name if account else "",
            }
        )
    return rows


def prepare(
    session: Session,
    tb: list[TbRow],
    mapping: dict[str, str],
    txn_date: date,
    previous: dict | None = None,
) -> Prepared:
    total = sum((r.balance for r in tb), Decimal(0))
    if total != 0:
        raise OpeningError(
            f"The trial balance does not balance: debits exceed credits by {total:,.2f}."
        )
    unmapped = [f"{r.number} {r.name}" for r in tb if r.number not in mapping and r.balance != 0]
    if unmapped:
        raise OpeningError("No QuickBooks account mapped for: " + "; ".join(unmapped))

    accounts = {a.id: a for a in session.scalars(select(QboAccount))}
    obe = next(
        (
            a
            for a in accounts.values()
            if a.fully_qualified_name == OBE_NAME and a.account_type == "Equity"
        ),
        None,
    )
    if obe is None:
        raise OpeningError(f"No '{OBE_NAME}' equity account in the QuickBooks cache; sync first.")

    warnings: list[str] = []
    per_account: dict[str, Decimal] = {}
    receivable = payable = Decimal(0)
    for row in tb:
        if row.balance == 0:
            continue
        account = accounts.get(mapping[row.number])
        if account is None:
            raise OpeningError(
                f"Mapped QuickBooks account {mapping[row.number]} ({row.number}) does not exist."
            )
        if not account.active:
            raise OpeningError(
                f"Mapped QuickBooks account {account.fully_qualified_name} is inactive."
            )
        if account.account_type == "Accounts Receivable":
            receivable += row.balance
            continue
        if account.account_type == "Accounts Payable":
            payable += row.balance
            continue
        per_account[account.id] = per_account.get(account.id, Decimal(0)) + row.balance

    if receivable or payable:
        per_account[obe.id] = per_account.get(obe.id, Decimal(0)) + receivable + payable

    closing = session.get(QboSetting, "closing_date")
    if closing and closing.value and txn_date.isoformat() <= closing.value:
        warnings.append(
            f"{txn_date} is on or before the QuickBooks closing date {closing.value}; posting will fail "
            "until a person moves the closing date."
        )

    true_up = previous is not None
    if true_up:
        for line in previous["Line"]:
            detail = line["JournalEntryLineDetail"]
            amount = Decimal(str(line["Amount"]))
            signed = amount if detail["PostingType"] == "Debit" else -amount
            aid = detail["AccountRef"]["value"]
            per_account[aid] = per_account.get(aid, Decimal(0)) - signed

    lines = [
        JournalLine(
            aid,
            accounts[aid].fully_qualified_name if aid in accounts else aid,
            amount.quantize(CENT),
            f"{'True-up' if true_up else 'Opening balance'} at {txn_date}",
        )
        for aid, amount in sorted(
            per_account.items(),
            key=lambda kv: accounts[kv[0]].fully_qualified_name if kv[0] in accounts else kv[0],
        )
        if amount.quantize(CENT) != 0
    ]
    if sum((ln.amount for ln in lines), Decimal(0)) != 0:
        raise OpeningError("Internal error: journal lines do not balance.")
    if not lines:
        warnings.append("Nothing to post: no differences.")
    return Prepared(txn_date, lines, receivable, payable, obe.id, warnings, true_up)


def post(session: Session, prepared: Prepared, confirm: str, qbo, actor: str) -> dict:
    if confirm != prepared.fingerprint():
        raise OpeningError(
            f"Fingerprint {confirm!r} does not match {prepared.fingerprint()!r}; the data changed since "
            "it was reviewed. Run prepare again."
        )
    if not prepared.lines:
        raise OpeningError("Nothing to post.")
    created = qbo.create(
        "JournalEntry", prepared.payload(), request_id=f"bob-opening-{prepared.fingerprint()}"
    )
    audit.record(
        session,
        "migration.opening_posted",
        "journal_entry",
        None,
        {
            "qbo_id": created["Id"],
            "date": prepared.txn_date.isoformat(),
            "fingerprint": prepared.fingerprint(),
            "true_up": prepared.true_up,
        },
        actor=actor,
    )
    return created


# --- command line ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import getpass

    from bob.qbo.client import QBOClient

    parser = argparse.ArgumentParser(prog="bob.migration.opening")
    parser.add_argument("command", choices=["suggest", "prepare", "post"])
    parser.add_argument("--tb", type=Path, required=True)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument(
        "--previous", type=Path, help="payload of an earlier opening entry (true-up)"
    )
    parser.add_argument("--confirm", help="fingerprint printed by prepare")
    args = parser.parse_args(argv)

    settings = get_settings()
    factory = make_sessionmaker(make_engine(settings.database_url))
    try:
        tb = read_trial_balance(args.tb)
        with factory() as session:
            if args.command == "suggest":
                rows = suggest_mapping(session, tb)
                out = args.out or Path("mapping.csv")
                with out.open("w", newline="") as f:
                    writer = csv.DictWriter(
                        f, fieldnames=["bc_account", "bc_name", "qbo_account_id", "qbo_name"]
                    )
                    writer.writeheader()
                    writer.writerows(rows)
                blanks = sum(1 for r in rows if not r["qbo_account_id"])
                print(
                    f"Wrote {out}: {len(rows)} accounts, {blanks} without a suggestion. Review every row."
                )
                return 0
            if not (args.mapping and args.date):
                print("prepare and post need --mapping and --date")
                return 1
            previous = json.loads(args.previous.read_text()) if args.previous else None
            prepared = prepare(session, tb, read_mapping(args.mapping), args.date, previous)
            print(prepared.report())
            payload_file = Path(f"{'trueup' if previous else 'opening'}-{args.date}.json")
            payload_file.write_text(json.dumps(prepared.payload(), indent=2))
            print(f"Payload written to {payload_file}")
            if args.command == "post":
                created = post(
                    session,
                    prepared,
                    args.confirm or "",
                    QBOClient(factory, settings),
                    f"cli:{getpass.getuser()}",
                )
                session.commit()
                print(f"Posted QuickBooks journal entry {created['Id']}.")
            return 0
    except OpeningError as err:
        print(f"Stopped: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
