"""Run the triage test set against the real model and score it.

    python -m evals.triage.build          # once, or after changing cases.py
    python -m evals.triage.run            # uses BOB_FOUNDRY_* and BOB_MODEL
    python -m evals.triage.run --only statement,quote

Each case goes through the real pipeline (ingestion, sender checks, duplicate detection,
triage) in a throwaway SQLite database. Costs a few cents per full run.
"""

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from bob.agent.llm import make_client
from bob.agent.triage import PROMPT_VERSION, triage_email
from bob.config import Settings, get_settings
from bob.db import Base, make_engine, make_sessionmaker
from bob.mail.ingest import poll_mailbox
from bob.mail.source import MailAttachment, MailMessage
from bob.models import InboundEmail
from bob.storage import LocalStorage
from evals.triage.build import DOCS
from evals.triage.cases import CASES, KNOWN_SENDERS, Case

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
RESULTS = Path(__file__).parent / "results"


@dataclass
class Scored:
    case: str
    passed: bool
    detail: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


class CaseMail:
    """Presents one or more cases as mailbox messages."""

    def __init__(self, cases: list[Case]):
        self.cases = {c.id: c for c in cases}
        self.processed: set[str] = set()

    def list_unprocessed(self, limit: int = 25) -> list[MailMessage]:
        messages = []
        for c in self.cases.values():
            if c.id in self.processed:
                continue
            headers = [("Authentication-Results", c.auth)] if c.auth else []
            messages.append(
                MailMessage(
                    id=c.id,
                    internet_message_id=f"<{c.id}@eval>",
                    conversation_id=c.id,
                    sender_address=c.sender,
                    sender_name=c.sender_name,
                    subject=c.subject,
                    body_text=c.body,
                    received_at=datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
                    headers=headers,
                )
            )
        return messages[:limit]

    def get_attachments(self, message_id: str) -> list[MailAttachment]:
        return [
            MailAttachment(d.filename, CONTENT_TYPES[d.kind], (DOCS / d.filename).read_bytes())
            for d in self.cases[message_id].attachments
        ]

    def mark_processed(self, message_id: str) -> None:
        self.processed.add(message_id)


def run_case(case: Case, base: Settings, client) -> Scored:
    with tempfile.TemporaryDirectory() as tmp:
        settings = base.model_copy(
            update={
                "database_url": f"sqlite:///{tmp}/eval.db",
                "local_storage_dir": f"{tmp}/blobs",
                "known_sender_addresses": KNOWN_SENDERS,
                "internal_domains": ["bridgewerk.ca"],
            }
        )
        engine = make_engine(settings.database_url)
        Base.metadata.create_all(engine)
        factory = make_sessionmaker(engine)
        storage = LocalStorage(settings.local_storage_dir)

        if case.received_before:  # ingest the earlier email first, without triaging it
            earlier = next(c for c in CASES if c.id == case.received_before)
            poll_mailbox(factory, CaseMail([earlier]), storage, settings)
        poll_mailbox(factory, CaseMail([case]), storage, settings)

        with factory() as session:
            email = session.scalar(
                select(InboundEmail).where(InboundEmail.graph_message_id == case.id)
            )
            triage_email(session, email.id, client, storage, settings)
            session.commit()
            email = session.get(InboundEmail, email.id)
            index_of = {a.id: i for i, a in enumerate(email.attachments)}
            got = {
                (index_of.get(d.attachment_id) if d.attachment_id else None): d
                for d in email.documents
            }
            scored = Scored(case.id, passed=True)
            if email.status == "held":
                scored.passed = False
                scored.detail.append("email held (model refused)")
            for exp in case.expect:
                doc = got.pop(exp.attachment, None)
                where = "body" if exp.attachment is None else f"attachment {exp.attachment}"
                if doc is None:
                    scored.passed = False
                    scored.detail.append(f"{where}: missing, expected {exp.doc_type}")
                    continue
                ok = doc.doc_type == exp.doc_type and doc.status in exp.statuses
                scored.passed &= ok
                scored.detail.append(
                    f"{where}: {'ok' if ok else 'WRONG'} got {doc.doc_type}/{doc.status}, "
                    f"expected {exp.doc_type}/{'|'.join(exp.statuses)}"
                    + (f" (question: {doc.question})" if doc.question else "")
                )
            scored.extras = [f"{k}: {d.doc_type}/{d.status} {d.summary}" for k, d in got.items()]
        engine.dispose()
        return scored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()

    cases = CASES
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in CASES if c.id in wanted]
    missing = [d.filename for c in cases for d in c.attachments if not (DOCS / d.filename).exists()]
    if missing:
        print("Documents missing; run: python -m evals.triage.build")
        return 1

    settings = get_settings()
    client = make_client(settings)
    results = []
    for case in cases:
        scored = run_case(case, settings, client)
        results.append(scored)
        print(f"{'PASS' if scored.passed else 'FAIL'}  {case.id:22} {case.description}")
        for line in scored.detail:
            print(f"        {line}")
        for line in scored.extras:
            print(f"        extra item: {line}")

    passed = sum(r.passed for r in results)
    print(
        f"\n{passed}/{len(results)} cases passed  (model {settings.model}, prompt {PROMPT_VERSION})"
    )
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{settings.model}.json"
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    print(f"Saved {out}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
