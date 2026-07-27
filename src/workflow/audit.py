from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from workflow.db.models import AuditEvent


def append_audit(
    session: Session,
    *,
    company_id: str,
    actor_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    request_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        company_id=company_id,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=request_id,
        before_state=before,
        after_state=after,
    )
    session.add(event)
    return event
