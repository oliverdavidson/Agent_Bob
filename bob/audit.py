from typing import Any

from sqlalchemy.orm import Session

from bob.models import AuditEvent

SYSTEM_ACTOR = "bob"


def record(
    session: Session,
    action: str,
    subject_type: str,
    subject_id: int | None,
    data: dict[str, Any] | None = None,
    actor: str = SYSTEM_ACTOR,
) -> AuditEvent:
    """Add an audit event to the current transaction, so it commits or rolls back with the
    change it describes."""
    event = AuditEvent(
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        data=data or {},
    )
    session.add(event)
    return event
