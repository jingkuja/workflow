from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow.db.models import Notification, NotificationStatus


def claim_notification(session: Session) -> Notification | None:
    now = datetime.now(UTC)
    item = session.scalar(
        select(Notification)
        .where(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
            Notification.next_retry_at <= now,
        )
        .order_by(Notification.next_retry_at, Notification.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if item is None:
        return None
    item.status = NotificationStatus.SENDING
    item.attempts += 1
    session.flush()
    return item


def mark_notification_sent(item: Notification, summary: str) -> None:
    item.status = NotificationStatus.SENT
    item.response_summary = summary[:1000]
    item.sent_at = datetime.now(UTC)


def mark_notification_failed(item: Notification, error: str, *, max_attempts: int) -> None:
    item.response_summary = error[:1000]
    if item.attempts >= max_attempts:
        item.status = NotificationStatus.DEAD
    else:
        item.status = NotificationStatus.FAILED
        item.next_retry_at = datetime.now(UTC) + timedelta(seconds=min(300, 2**item.attempts))
