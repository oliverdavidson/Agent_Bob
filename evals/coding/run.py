"""Run the coding test set against the real model and score it.

    python -m evals.triage.build        # documents are shared with the triage set
    python -m evals.coding.run
    python -m evals.coding.run --only legal-acquisition,insurance-annual

Each case is ingested into a throwaway database seeded with evals/coding/chart.py, marked as a
triaged document, and coded by the real pipeline (model, second opinion, validator).
"""

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from bob.agent.coding import PROMPT_VERSION, code_document
from bob.config import Settings, get_settings
from bob.db import Base, make_engine, make_sessionmaker
from bob.mail.ingest import poll_mailbox
from bob.models import Document, InboundEmail, Proposal
from bob.storage import LocalStorage
from evals.coding import chart
from evals.coding.cases import CASES, CodingCase
from evals.triage.build import DOCS
from evals.triage.cases import CASES as TRIAGE_CASES
from evals.triage.cases import KNOWN_SENDERS
from evals.triage.run import CaseMail

TODAY = date(2026, 10, 21)
RESULTS = Path(__file__).parent / "results"
DOC_TYPE = {"bill": "vendor_invoice", "expense": "receipt", "credit": "credit_note"}


@dataclass
class Scored:
    case: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""


def score(case: CodingCase, proposal: Proposal | None, doc: Document) -> Scored:
    if proposal is None:
        ok = case.ambiguous and doc.status == "needs_human"
        return Scored(
            case.triage_case,
            ok,
            {"proposal": ok},
            f"no proposal; document {doc.status}: {doc.question}",
        )
    e = proposal.entry
    accounts = frozenset(line["account_id"] for line in e["lines"])
    taxes = frozenset(line["tax_code_id"] for line in e["lines"])
    checks = {
        "kind": e["kind"] == case.kind,
        "accounts": accounts in case.accounts,
        "tax_codes": taxes in case.tax_codes,
        "tags": case.required_tags <= set(e["tags"]),
        "currency": e["currency"] == case.currency,
        "vendor": e.get("vendor_id") == case.vendor_id,
        "status": proposal.status in case.statuses,
    }
    if case.extract.get("total"):
        checks["total"] = Decimal(e["total"]) == Decimal(case.extract["total"])
    if case.extract.get("invoice_number"):
        checks["invoice_number"] = (e.get("invoice_number") or "") == case.extract["invoice_number"]
    detail = (
        f"{e['kind']} accounts={sorted(accounts)} tax={sorted(taxes)} tags={e['tags']} "
        f"total={e['total']} vendor={e.get('vendor_id')} -> {proposal.status}"
        + (f" ({proposal.question})" if proposal.question else "")
    )
    return Scored(case.triage_case, all(checks.values()), checks, detail)


def run_case(case: CodingCase, base: Settings, client) -> Scored:
    triage_case = next(c for c in TRIAGE_CASES if c.id == case.triage_case)
    with tempfile.TemporaryDirectory() as tmp:
        settings = base.model_copy(
            update={
                "database_url": f"sqlite:///{tmp}/eval.db",
                "local_storage_dir": f"{tmp}/blobs",
                "known_sender_addresses": KNOWN_SENDERS,
                "internal_domains": ["bridgewerk.ca"],
                "reviewer_addresses": [],
            }
        )
        engine = make_engine(settings.database_url)
        Base.metadata.create_all(engine)
        factory = make_sessionmaker(engine)
        storage = LocalStorage(settings.local_storage_dir)
        with factory() as s:
            chart.seed(s)
            s.commit()
        poll_mailbox(factory, CaseMail([triage_case]), storage, settings)
        with factory() as s:
            email = s.scalar(
                select(InboundEmail).where(InboundEmail.graph_message_id == triage_case.id)
            )
            att = email.attachments[case.attachment]
            doc = Document(
                email=email,
                attachment_id=att.id,
                doc_type=DOC_TYPE[case.kind],
                status="ready_to_code",
                counterparty=case.extract.get("vendor"),
                summary=triage_case.description,
            )
            s.add(doc)
            s.commit()
            proposal = code_document(s, doc.id, client, storage, settings, today=TODAY)
            s.commit()
            result = score(case, proposal, s.get(Document, doc.id))
        engine.dispose()
        return result


def main() -> int:
    from bob.agent.llm import make_client

    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()
    cases = [c for c in CASES if not args.only or c.triage_case in set(args.only.split(","))]
    if not DOCS.exists():
        print("Documents missing; run: python -m evals.triage.build")
        return 1

    settings = get_settings()
    client = make_client(settings)
    results = []
    for case in cases:
        scored = run_case(case, settings, client)
        results.append(scored)
        failed = [k for k, v in scored.checks.items() if not v]
        print(f"{'PASS' if scored.passed else 'FAIL'}  {case.triage_case:22} {scored.detail}")
        if failed:
            print(
                f"        wrong: {', '.join(failed)}  ({case.note})"
                if case.note
                else f"        wrong: {', '.join(failed)}"
            )

    passed = sum(r.passed for r in results)
    field_totals: dict[str, list[bool]] = {}
    for r in results:
        for k, v in r.checks.items():
            field_totals.setdefault(k, []).append(v)
    print(
        f"\n{passed}/{len(results)} cases fully correct  (model {settings.model}, prompt {PROMPT_VERSION})"
    )
    print("By field: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in field_totals.items()))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{settings.model}.json"
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    print(f"Saved {out}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
