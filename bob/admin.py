"""Operator commands. Run inside the container (az containerapp exec) or locally.

python -m bob.admin status
python -m bob.admin pause                 # stop all posting now
python -m bob.admin resume                # resume, and retry proposals left approved
python -m bob.admin approve 42            # post a held proposal
python -m bob.admin reject 42 "not ours"
python -m bob.admin undo --id 42 --id 43             # dry run: shows what would go
python -m bob.admin undo --vendor "Bow River" --since 2026-10-01 --execute
"""

import argparse
import getpass
import sys
from datetime import date

from sqlalchemy import func, select

from bob import jobs
from bob.accounting import posting
from bob.config import get_settings
from bob.db import make_engine, make_sessionmaker
from bob.models import Proposal
from bob.qbo.client import QBOClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bob.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p = sub.add_parser("approve")
    p.add_argument("proposal_id", type=int)
    p = sub.add_parser("reject")
    p.add_argument("proposal_id", type=int)
    p.add_argument("reason")
    p = sub.add_parser("undo")
    p.add_argument("--id", dest="ids", type=int, action="append")
    p.add_argument("--vendor")
    p.add_argument("--since", type=date.fromisoformat)
    p.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args(argv)

    settings = get_settings()
    factory = make_sessionmaker(make_engine(settings.database_url))
    actor = f"cli:{getpass.getuser()}"

    with factory() as session:
        if args.command == "status":
            counts = dict(
                session.execute(
                    select(Proposal.status, func.count()).group_by(Proposal.status)
                ).all()
            )
            print(f"Posting paused: {posting.posting_paused(session)}")
            print(f"QuickBooks writes enabled: {settings.qbo_writes_enabled}")
            for status, n in sorted(counts.items()):
                print(f"  {status:12} {n}")
            return 0

        if args.command == "pause":
            posting.set_posting_paused(session, True, actor)
            session.commit()
            print("Posting paused.")
            return 0

        if args.command == "resume":
            posting.set_posting_paused(session, False, actor)
            waiting = session.scalars(select(Proposal).where(Proposal.status == "approved")).all()
            for p in waiting:
                jobs.enqueue(session, "post_proposal", {"proposal_id": p.id})
            session.commit()
            print(f"Posting resumed; {len(waiting)} approved proposal(s) queued.")
            return 0

        if args.command == "approve":
            posting.approve(session, args.proposal_id, actor)
            session.commit()
            print(f"Proposal {args.proposal_id} approved and queued for posting.")
            return 0

        if args.command == "reject":
            posting.reject(session, args.proposal_id, actor, args.reason)
            session.commit()
            print(f"Proposal {args.proposal_id} rejected.")
            return 0

        if args.command == "undo":
            if not (args.ids or args.vendor or args.since):
                print("Give --id, --vendor or --since.")
                return 1
            targets = posting.find_posted(
                session, since=args.since, vendor=args.vendor, ids=args.ids
            )
            for p in targets:
                print(
                    f"  proposal {p.id}: {p.entry['vendor_name']} {p.entry.get('invoice_number') or ''} "
                    f"{p.entry['total']} -> {p.qbo_entity} {p.qbo_id}"
                )
            if not args.execute:
                print(
                    f"Dry run: {len(targets)} entr{'y' if len(targets) == 1 else 'ies'} would be undone. "
                    "Add --execute to proceed."
                )
                return 0
            qbo = QBOClient(factory, settings)
            failures = 0
            for p in targets:
                try:
                    result = posting.reverse(session, p.id, qbo, actor, reason="bulk undo via CLI")
                    session.commit()
                    print(f"  undone {p.id}: {result.message}")
                except posting.UndoRefused as err:
                    session.rollback()
                    failures += 1
                    print(f"  refused {p.id}: {err}")
            return 1 if failures else 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
