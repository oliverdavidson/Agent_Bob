"""A small work queue on Postgres.

Keeping jobs in the same database as workflow state means a state change and the job it
triggers commit in one transaction: no lost or duplicated work between two systems.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bob.db import utcnow
from bob.models import Job

STALE_AFTER = timedelta(minutes=15)


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    delay: timedelta | None = None,
) -> Job | None:
    """Queue a job in the caller's transaction. Returns None if dedupe_key was already used."""
    job = Job(kind=kind, payload=payload, dedupe_key=dedupe_key)
    if delay:
        job.run_after = utcnow() + delay
    if dedupe_key is None:
        session.add(job)
        return job
    try:
        with session.begin_nested():
            session.add(job)
    except IntegrityError:
        return None
    return job


def claim(session: Session, kinds: list[str] | None = None) -> Job | None:
    """Lock and return the next runnable job, or None. Commit to release it to other workers."""
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= utcnow())
        .order_by(Job.id)
        .limit(1)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.locked_at = utcnow()
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.locked_at = None
    job.last_error = None


def fail(session: Session, job: Job, error: str) -> None:
    """Retry with exponential backoff, or give up after max_attempts."""
    job.last_error = error[:4000]
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = "failed"
    else:
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=30 * 2 ** (job.attempts - 1))


def requeue_stale(session: Session) -> int:
    """Return jobs abandoned by a crashed worker to the queue."""
    result = session.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < utcnow() - STALE_AFTER)
        .values(status="queued", locked_at=None)
    )
    return result.rowcount or 0
