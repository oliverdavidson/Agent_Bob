"""Runs every triage test case through the real pipeline with a scripted model.

This checks the eval harness and the rules code enforces on its own. In particular, for the
fraud cases the scripted model does NOT raise a flag, so passing proves the code catches them.
"""

import pytest

from bob.agent.triage import TriagedItem, TriageResult
from evals.triage.build import DOCS
from evals.triage.cases import CASES, HUMAN
from evals.triage.run import run_case
from tests.conftest import FakeClaude

# Cases where sender checks in code must force a person to look, whatever the model says.
CODE_ENFORCED = {"unknown-sender", "lookalike-domain", "spoofed-internal"}


def scripted_answer(case) -> TriageResult:
    items = []
    for exp in case.expect:
        if exp.doc_type == "duplicate":
            continue  # code handles duplicates before the model is called
        model_flags = exp.statuses == HUMAN and case.id not in CODE_ENFORCED
        items.append(
            TriagedItem(
                attachment_index=exp.attachment,
                doc_type=exp.doc_type,
                counterparty=None,
                summary=f"scripted {exp.doc_type}",
                needs_human=model_flags,
                question="Scripted question?" if model_flags else None,
            )
        )
    return TriageResult(items=items)


@pytest.mark.skipif(not DOCS.exists(), reason="run python -m evals.triage.build")
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case_passes_with_scripted_model(case, settings):
    scored = run_case(case, settings, FakeClaude(scripted_answer(case)))
    assert scored.passed, scored.detail
    assert scored.extras == []


def test_cases_are_well_formed():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    assert len(CASES) >= 24
    for case in CASES:
        indexes = [e.attachment for e in case.expect if e.attachment is not None]
        assert all(0 <= i < len(case.attachments) for i in indexes), case.id
        if case.received_before:
            assert case.received_before in ids
