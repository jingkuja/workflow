from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from workflow.db.models import (
    ActorProfile,
    Role,
    TaskAssignment,
    TaskNumberCounter,
)
from workflow.errors import NoEligibleAssignee
from workflow.t2.calendar import week_start_for


def advisory_lock(session: Session, key: str) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": digest})


def next_task_number(session: Session, created_at: datetime, timezone: str) -> str:
    local_date = created_at.astimezone(ZoneInfo(timezone)).date()
    advisory_lock(session, f"task-number:{local_date.isoformat()}")
    counter = session.get(TaskNumberCounter, local_date)
    if counter is None:
        counter = TaskNumberCounter(counter_date=local_date, last_value=0)
        session.add(counter)
    counter.last_value += 1
    session.flush()
    return f"WJ-{local_date:%Y%m%d}-{counter.last_value:04d}"


def employee_loads(
    session: Session,
    company_id: str,
    at: datetime,
    timezone: str,
) -> list[tuple[ActorProfile, int]]:
    week_start = week_start_for(at, timezone)
    employees = session.scalars(
        select(ActorProfile)
        .where(
            ActorProfile.company_id == company_id,
            ActorProfile.role == Role.EMPLOYEE,
            ActorProfile.position == "新媒体运营",
            ActorProfile.active.is_(True),
        )
        .order_by(ActorProfile.display_name, ActorProfile.id)
    ).all()
    counts = dict(
        session.execute(
            select(
                TaskAssignment.assignee_id,
                func.coalesce(func.sum(TaskAssignment.workload_delta), 0),
            )
            .where(
                TaskAssignment.company_id == company_id,
                TaskAssignment.work_week_start == week_start,
            )
            .group_by(TaskAssignment.assignee_id)
        ).all()
    )
    return [(employee, int(counts.get(employee.id, 0))) for employee in employees]


def choose_employee(
    loads: list[tuple[ActorProfile, int]],
    chooser: Callable[[list[ActorProfile]], ActorProfile] = secrets.choice,
) -> ActorProfile:
    if not loads:
        raise NoEligibleAssignee("当前没有可分配的在岗新媒体运营员工。")
    minimum = min(count for _, count in loads)
    candidates = [employee for employee, count in loads if count == minimum]
    return chooser(candidates)
