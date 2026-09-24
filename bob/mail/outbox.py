"""What Bob emails: one question per item that needs a person, and a daily digest.

Everything goes only to BOB_REVIEWER_ADDRESSES. Bob never writes to vendors or other senders.
"""

import re
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bob import audit
from bob.config import Settings
from bob.db import as_utc, utcnow
from bob.mail.source import MailSender
from bob.models import Control, Document, InboundEmail, Job, Proposal, QboAccount, Question

TOKEN = re.compile(r"\[Bob ([PD])(\d+)\]")
REPLY_HELP = (
    "Reply to this email with one of:\n"
    "  approve                 book it as proposed\n"
    "  reject: <reason>        don't book it\n"
    '  or say what to change, e.g. "code it to Legal Fees" or "this is for Aurora, not us"\n'
)


def parse_token(subject: str) -> tuple[str, int] | None:
    match = TOKEN.search(subject or "")
    return (match.group(1), int(match.group(2))) if match else None


def _money(value: str | Decimal) -> str:
    return f"{Decimal(value):,.2f}"


def _proposal_text(session: Session, p: Proposal) -> str:
    accounts = {a.id: a.fully_qualified_name for a in session.scalars(select(QboAccount))}
    e = p.entry
    lines = [
        f"{e['vendor_name']}  {e.get('invoice_number') or '(no number)'}  dated {e['invoice_date']}",
        f"{e['kind']}: {e['currency']} {_money(e['total'])} ({_money(e['subtotal'])} + tax {_money(e['tax_total'])})",
        "Proposed coding:",
    ]
    lines += [
        f"  {accounts.get(line['account_id'], line['account_id'])}: {_money(line['amount'])} "
        f"(tax code {line['tax_code_id']}) {line.get('description', '')}".rstrip()
        for line in e["lines"]
    ]
    if e.get("tags"):
        lines.append(f"Tags: {', '.join(e['tags'])}")
    if p.rationale:
        lines.append(f"Why: {p.rationale}")
    for f in p.findings:
        lines.append(f"  [{f['severity']}] {f['message']}")
    return "\n".join(lines)


def _open_question_exists(session: Session, token: str) -> bool:
    return (
        session.scalar(
            select(Question.id).where(Question.token == token, Question.status == "open")
        )
        is not None
    )


def ask_questions(session: Session, sender: MailSender, settings: Settings) -> int:
    """Email a question for every held proposal and every document waiting on a person."""
    if not settings.reviewer_addresses:
        return 0
    sent = 0

    for p in session.scalars(
        select(Proposal).where(Proposal.status == "held").order_by(Proposal.id)
    ):
        token = f"P{p.id}"
        if _open_question_exists(session, token):
            continue
        question = p.question or "This needs your approval before it is booked."
        subject = f"[Bob {token}] {p.entry['vendor_name']} {p.entry['currency']} {_money(p.entry['total'])}: {question[:80]}"
        body = f"{question}\n\n{_proposal_text(session, p)}\n\n{REPLY_HELP}"
        sent += _send(session, sender, settings, token, subject, body, proposal_id=p.id)

    for d in session.scalars(
        select(Document).where(Document.status == "needs_human").order_by(Document.id)
    ):
        token = f"D{d.id}"
        if _open_question_exists(session, token):
            continue
        email = session.get(InboundEmail, d.email_id)
        question = d.question or "What should Bob do with this?"
        subject = f"[Bob {token}] {d.counterparty or email.sender_address}: {question[:80]}"
        body = (
            f"{question}\n\n{d.summary}\n"
            f"From: {email.sender_name or ''} <{email.sender_address}> (trust: {email.sender_trust}, "
            f"authentication: {email.sender_auth})\nSubject: {email.subject}\n"
            f"Received: {email.received_at:%Y-%m-%d %H:%M} UTC\n\n"
            "Reply 'approve' to have Bob code and book it, 'reject: <reason>' to ignore it, "
            "or tell Bob what it is.\n"
        )
        sent += _send(session, sender, settings, token, subject, body, document_id=d.id)
    return sent


def _send(
    session, sender, settings, token, subject, body, proposal_id=None, document_id=None
) -> int:
    sender.send(settings.reviewer_addresses, subject, body)
    q = Question(
        token=token,
        proposal_id=proposal_id,
        document_id=document_id,
        sent_to=list(settings.reviewer_addresses),
        subject=subject,
        body=body,
    )
    session.add(q)
    session.flush()
    audit.record(session, "question.sent", "question", q.id, {"token": token, "to": q.sent_to})
    return 1


# --- digest ---------------------------------------------------------------------------------


def _control(session: Session, key: str) -> str | None:
    c = session.get(Control, key)
    return c.value if c else None


def digest_due(session: Session, settings: Settings, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return (
        now.hour >= settings.digest_hour_utc
        and _control(session, "last_digest_date") != now.date().isoformat()
    )


def build_digest(session: Session, since: datetime, now: datetime) -> tuple[str, str]:
    posted = [
        p
        for p in session.scalars(select(Proposal).where(Proposal.status == "posted"))
        if p.posted_at and as_utc(p.posted_at) >= since
    ]
    flagged = [p for p in posted if p.outcome == "post_and_flag"]
    held = session.scalars(select(Proposal).where(Proposal.status == "held")).all()
    broken = session.scalars(
        select(Proposal).where(Proposal.status.in_(("needs_fix", "failed")))
    ).all()
    approved_waiting = session.scalars(select(Proposal).where(Proposal.status == "approved")).all()
    waiting_docs = session.scalars(select(Document).where(Document.status == "needs_human")).all()
    held_emails = session.scalars(select(InboundEmail).where(InboundEmail.status == "held")).all()
    failed_jobs = session.scalars(select(Job).where(Job.status == "failed")).all()

    total = sum((Decimal(p.entry["total"]) for p in posted), Decimal(0))
    subject = (
        f"Bob daily digest {now:%Y-%m-%d}: posted {len(posted)} (${total:,.2f}), "
        f"{len(held) + len(waiting_docs)} waiting on you"
    )
    out = [f"Since {since:%Y-%m-%d %H:%M} UTC.\n"]

    def section(title: str, rows: list[str]) -> None:
        if rows:
            out.append(f"{title} ({len(rows)})")
            out.extend(f"  {r}" for r in rows)
            out.append("")

    def describe(p: Proposal) -> str:
        e = p.entry
        return f"P{p.id} {e['vendor_name']} {e.get('invoice_number') or ''} {e['currency']} {_money(e['total'])}".replace(
            "  ", " "
        )

    section("Posted", [f"{describe(p)} -> {p.qbo_entity} {p.qbo_id}" for p in posted])
    section(
        "Posted with flags: confirm before month-end",
        [
            f"{describe(p)}: "
            + "; ".join(f["message"] for f in p.findings if f["severity"] == "flag")
            for p in flagged
        ],
    )
    section(
        "Waiting on your reply",
        [f"{describe(p)}: {p.question or 'needs approval'}" for p in held]
        + [f"D{d.id} {d.counterparty or ''}: {d.question}" for d in waiting_docs],
    )
    section(
        "Could not be booked",
        [
            f"{describe(p)}: {p.last_error or '; '.join(f['message'] for f in p.findings if f['severity'] == 'error')}"
            for p in broken
        ],
    )
    section(
        "Approved but not posted (writes off, paused, or not connected)",
        [f"{describe(p)}: {p.last_error or ''}" for p in approved_waiting],
    )
    section("Emails held", [f"{e.sender_address}: {e.subject}" for e in held_emails])
    section(
        "Background jobs that gave up",
        [f"{j.kind} #{j.id}: {(j.last_error or '')[:120]}" for j in failed_jobs],
    )
    if len(out) == 1:
        out.append("Nothing happened. All quiet.")
    out.append("To undo anything Bob posted: python -m bob.admin undo --id <proposal id>")
    return subject, "\n".join(out)


def send_digest(
    session: Session, sender: MailSender, settings: Settings, now: datetime | None = None
) -> bool:
    now = now or utcnow()
    if not settings.reviewer_addresses or not digest_due(session, settings, now):
        return False
    last = _control(session, "last_digest_at")
    since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
    subject, body = build_digest(session, since, now)
    sender.send(settings.reviewer_addresses, subject, body)
    session.merge(Control(key="last_digest_date", value=now.date().isoformat(), updated_by="bob"))
    session.merge(Control(key="last_digest_at", value=now.isoformat(), updated_by="bob"))
    audit.record(session, "digest.sent", "digest", None, {"subject": subject})
    return True
