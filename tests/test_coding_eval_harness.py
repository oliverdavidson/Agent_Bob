"""Runs every coding test case through the real pipeline with a scripted model that gives the
expected answer. Checks the harness, and that the validator and routing produce the expected
status (approved or held) for each correct booking."""

from decimal import Decimal

import pytest

from bob.agent.coding import CodedLine, CodingResult
from evals.coding.cases import CASES
from evals.coding.run import run_case
from evals.triage.build import DOCS
from tests.conftest import FakeClaude


def scripted(case) -> CodingResult:
    x = case.extract
    subtotal, tax = Decimal(x["subtotal"]), Decimal(x.get("tax", "0"))
    account = min(case.accounts[0])
    tax_code = min(case.tax_codes[0])
    return CodingResult(
        kind=case.kind,
        vendor_name=x.get("vendor", "Unknown"),
        vendor_id=case.vendor_id,
        invoice_number=x.get("invoice_number"),
        invoice_date=x.get("invoice_date", "2026-10-08"),
        due_date=None,
        currency=case.currency,
        subtotal=f"{subtotal:.2f}",
        tax_total=f"{tax:.2f}",
        total=f"{subtotal + tax:.2f}",
        supplier_tax_number=x.get("tax_number"),
        service_start="2026-10-01" if "insurance" in case.triage_case else None,
        service_end="2027-09-30" if "insurance" in case.triage_case else None,
        lines=[
            CodedLine(
                description="scripted",
                account_id=account,
                amount=f"{subtotal:.2f}",
                tax_code_id=tax_code,
                tax_amount=f"{tax:.2f}",
            )
        ],
        tags=sorted(case.required_tags),
        rationale="scripted",
        ambiguous=case.ambiguous,
        question="Scripted question?" if case.ambiguous else None,
    )


@pytest.mark.skipif(not DOCS.exists(), reason="run python -m evals.triage.build")
@pytest.mark.parametrize("case", CASES, ids=[c.triage_case for c in CASES])
def test_expected_answer_scores_as_correct(case, settings):
    answer = scripted(case)
    scored = run_case(case, settings, FakeClaude([answer, answer]))
    assert scored.passed, (scored.checks, scored.detail)


def test_wrong_answer_is_caught(settings):
    case = next(c for c in CASES if c.triage_case == "legal-acquisition")
    wrong = scripted(case).model_copy(
        update={
            "lines": [
                CodedLine(
                    description="x",
                    account_id="61",
                    amount="25750.00",
                    tax_code_id="4",
                    tax_amount="1287.50",
                )
            ],
            "tags": [],
        }
    )
    scored = run_case(case, settings, FakeClaude([wrong, wrong]))
    assert not scored.passed
    assert scored.checks["accounts"] is False and scored.checks["tags"] is False
