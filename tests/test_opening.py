import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bob.migration.opening import (
    OpeningError,
    parse_amount,
    post,
    prepare,
    read_mapping,
    read_trial_balance,
    suggest_mapping,
)
from bob.models import AuditEvent, QboAccount, QboSetting

CUTOVER = date(2026, 9, 30)
TB = """No.,Name,Debit,Credit
1000,Cash - Operating,"286,325.68",
1200,Accounts Receivable,85000.00,
1500,Prepaid Expenses,4200.00,
2100,Accounts Payable,,18420.33
2300,GST Payable,,1204.50
3000,Share Capital,,100.00
3900,Retained Earnings,,355800.85
,Total,"375,525.68","375,525.68"
"""
MAPPING = """bc_account,qbo_account_id
1000,10
1200,12
1500,15
2100,21
2300,23
3000,30
3900,39
"""


def seed(s):
    s.add_all(
        [
            QboAccount(
                id="10", name="Cash", fully_qualified_name="Cash - Operating", account_type="Bank"
            ),
            QboAccount(
                id="12",
                name="AR",
                fully_qualified_name="Accounts Receivable (A/R)",
                account_type="Accounts Receivable",
            ),
            QboAccount(
                id="15",
                name="Prepaid",
                fully_qualified_name="Prepaid Expenses",
                account_type="Other Current Asset",
            ),
            QboAccount(
                id="21",
                name="AP",
                fully_qualified_name="Accounts Payable (A/P)",
                account_type="Accounts Payable",
            ),
            QboAccount(
                id="23",
                name="GST",
                fully_qualified_name="GST/HST Payable",
                account_type="Other Current Liability",
            ),
            QboAccount(
                id="30",
                name="Share Capital",
                fully_qualified_name="Share Capital",
                account_type="Equity",
            ),
            QboAccount(
                id="39", name="RE", fully_qualified_name="Retained Earnings", account_type="Equity"
            ),
            QboAccount(
                id="31",
                name="OBE",
                fully_qualified_name="Opening Balance Equity",
                account_type="Equity",
            ),
        ]
    )


@pytest.fixture
def files(tmp_path) -> tuple[Path, Path]:
    tb, mapping = tmp_path / "tb.csv", tmp_path / "mapping.csv"
    tb.write_text(TB)
    mapping.write_text(MAPPING)
    return tb, mapping


@pytest.fixture
def session(factory):
    with factory() as s:
        seed(s)
        s.commit()
        yield s


def lines_by_account(prepared) -> dict[str, Decimal]:
    return {ln.account_id: ln.amount for ln in prepared.lines}


def test_reads_bc_trial_balance_and_skips_totals(files):
    rows = read_trial_balance(files[0])
    assert len(rows) == 7
    assert rows[0].balance == Decimal("286325.68")
    assert rows[3].balance == Decimal("-18420.33")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,234.56", "1234.56"),
        ("(500.00)", "-500.00"),
        ("75.10-", "-75.10"),
        ("", "0"),
        ("$12", "12"),
    ],
)
def test_parse_amount(text, value):
    assert parse_amount(text) == Decimal(value)


def test_balance_column_format(tmp_path):
    path = tmp_path / "tb2.csv"
    path.write_text(
        "G/L Account No.,G/L Account Name,Balance at Date\n1000,Cash,100.00\n3000,Equity,-100.00\n"
    )
    assert [r.balance for r in read_trial_balance(path)] == [Decimal("100.00"), Decimal("-100.00")]


def test_prepare_leaves_ar_ap_to_open_items_via_obe(session, files):
    prepared = prepare(session, read_trial_balance(files[0]), read_mapping(files[1]), CUTOVER)
    by_account = lines_by_account(prepared)
    assert "12" not in by_account and "21" not in by_account
    assert prepared.receivable == Decimal("85000.00")
    assert prepared.payable == Decimal("-18420.33")
    assert by_account["31"] == Decimal("66579.67")  # AR - AP parked in Opening Balance Equity
    assert sum(by_account.values()) == 0
    payload = prepared.payload()
    assert payload["DocNumber"] == "OPENING-20260930"
    cash = next(
        ln for ln in payload["Line"] if ln["JournalEntryLineDetail"]["AccountRef"]["value"] == "10"
    )
    assert cash["JournalEntryLineDetail"]["PostingType"] == "Debit" and cash["Amount"] == 286325.68
    assert "Fingerprint" in prepared.report()


def test_imbalanced_trial_balance_is_refused(session, files, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(TB.replace("286,325.68", "286,325.67"))
    with pytest.raises(OpeningError, match="does not balance"):
        prepare(session, read_trial_balance(path), read_mapping(files[1]), CUTOVER)


def test_unmapped_account_is_refused(session, files, tmp_path):
    mapping = tmp_path / "m.csv"
    mapping.write_text(MAPPING.replace("2300,23\n", ""))
    with pytest.raises(OpeningError, match="2300 GST Payable"):
        prepare(session, read_trial_balance(files[0]), read_mapping(mapping), CUTOVER)


def test_closed_period_warning(session, files):
    session.add(QboSetting(key="closing_date", value="2026-09-30"))
    session.flush()
    prepared = prepare(session, read_trial_balance(files[0]), read_mapping(files[1]), CUTOVER)
    assert any("closing date" in w for w in prepared.warnings)


def test_true_up_posts_only_the_difference(session, files, tmp_path):
    first = prepare(session, read_trial_balance(files[0]), read_mapping(files[1]), CUTOVER)
    final = tmp_path / "final.csv"
    # September close adds an accrual: prepaid down 300, retained earnings absorbs it
    final.write_text(
        TB.replace("4200.00", "3900.00")
        .replace("355800.85", "355500.85")
        .replace("375,525.68", "375,225.68")
    )
    trueup = prepare(
        session,
        read_trial_balance(final),
        read_mapping(files[1]),
        CUTOVER,
        previous=first.payload(),
    )
    assert lines_by_account(trueup) == {"15": Decimal("-300.00"), "39": Decimal("300.00")}
    assert trueup.payload()["DocNumber"] == "TRUEUP-20260930"


def test_post_requires_matching_fingerprint(session, files):
    class Recorder:
        def __init__(self):
            self.calls = []

        def create(self, entity, payload, request_id):
            self.calls.append((entity, payload, request_id))
            return {"Id": "88"}

    prepared = prepare(session, read_trial_balance(files[0]), read_mapping(files[1]), CUTOVER)
    qbo = Recorder()
    with pytest.raises(OpeningError, match="does not match"):
        post(session, prepared, "0000", qbo, "cli:oliver")
    assert qbo.calls == []

    created = post(session, prepared, prepared.fingerprint(), qbo, "cli:oliver")
    assert created["Id"] == "88"
    entity, payload, request_id = qbo.calls[0]
    assert entity == "JournalEntry" and request_id == f"bob-opening-{prepared.fingerprint()}"
    assert json.dumps(payload) == json.dumps(prepared.payload())
    session.flush()
    assert session.query(AuditEvent).filter_by(action="migration.opening_posted").count() == 1


def test_suggest_mapping_matches_names(session, files):
    rows = {r["bc_account"]: r for r in suggest_mapping(session, read_trial_balance(files[0]))}
    assert rows["1500"]["qbo_account_id"] == "15"
    assert rows["3000"]["qbo_account_id"] == "30"
