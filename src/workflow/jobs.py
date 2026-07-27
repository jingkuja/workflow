from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from workflow.db.models import BackgroundJob, JobStatus


def claim_job(session: Session, *, worker_id: str, lease_seconds: int) -> BackgroundJob | None:
    now = datetime.now(UTC)
    query = (
        select(BackgroundJob)
        .where(
            or_(
                (
                    (BackgroundJob.status.in_([JobStatus.PENDING, JobStatus.FAILED]))
                    & (BackgroundJob.available_at <= now)
                ),
                ((BackgroundJob.status == JobStatus.RUNNING) & (BackgroundJob.lease_until < now)),
            )
        )
        .order_by(BackgroundJob.available_at, BackgroundJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = session.scalar(query)
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.leased_by = worker_id
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    session.flush()
    return job


def succeed_job(job: BackgroundJob) -> None:
    job.status = JobStatus.SUCCEEDED
    job.completed_at = datetime.now(UTC)
    job.lease_until = None


def fail_job(job: BackgroundJob, error: str, *, permanent: bool = False) -> None:
    now = datetime.now(UTC)
    job.last_error = error[:2000]
    job.lease_until = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
        job.completed_at = now
    else:
        job.status = JobStatus.FAILED
        job.available_at = now + timedelta(seconds=min(300, 2**job.attempts))
