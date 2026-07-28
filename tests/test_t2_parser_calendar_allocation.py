from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from workflow.db.models import ActorProfile, Base, Role, TaskAssignment
from workflow.t2.allocation import choose_employee, employee_loads
from workflow.t2.calendar import effective_started_at, week_start_for
from workflow.t2.parser import parse_topic_document


def test_real_topic_sample_parses_exactly_ten_topics() -> None:
    content = Path("docs/AI行业选题文档上传样例.docx").read_bytes()

    topics = parse_topic_document(content)

    assert len(topics) == 10
    assert [topic.source_sequence for topic in topics] == list(range(1, 11))
    assert topics[0].title.startswith("芯片公司先给客户50亿美元")
    assert all("提词器口播正文" in "".join(topic.source_content["fields"]) for topic in topics)


def test_effective_started_at_business_boundaries() -> None:
    zone = ZoneInfo("Asia/Shanghai")

    monday_early = datetime(2026, 7, 27, 8, 30, tzinfo=zone)
    monday_working = datetime(2026, 7, 27, 10, 0, tzinfo=zone)
    friday_late = datetime(2026, 7, 31, 18, 0, tzinfo=zone)
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=zone)

    assert effective_started_at(monday_early).hour == 9
    assert effective_started_at(monday_working) == monday_working
    assert effective_started_at(friday_late) == datetime(2026, 8, 3, 9, 0, tzinfo=zone)
    assert effective_started_at(saturday) == datetime(2026, 8, 3, 9, 0, tzinfo=zone)


def test_weekly_load_uses_append_only_deltas_and_minimum_choice() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)
    week_start = week_start_for(now)
    with Session(engine) as session:
        a = ActorProfile(
            company_id="company",
            display_name="员工甲",
            role=Role.EMPLOYEE,
            position="新媒体运营",
            active=True,
            token_sha256="a" * 64,
        )
        b = ActorProfile(
            company_id="company",
            display_name="员工乙",
            role=Role.EMPLOYEE,
            position="新媒体运营",
            active=True,
            token_sha256="b" * 64,
        )
        session.add_all([a, b])
        session.flush()
        session.add_all(
            [
                TaskAssignment(
                    company_id="company",
                    task_id="task-a",
                    assignee_id=a.id,
                    event_type="AUTO_ASSIGNED",
                    workload_delta=1,
                    work_week_start=week_start,
                    assigned_at=now,
                ),
                TaskAssignment(
                    company_id="company",
                    task_id="task-a",
                    assignee_id=a.id,
                    event_type="CANCEL_REVERSED",
                    workload_delta=-1,
                    work_week_start=week_start,
                    assigned_at=now,
                ),
                TaskAssignment(
                    company_id="company",
                    task_id="task-b",
                    assignee_id=b.id,
                    event_type="AUTO_ASSIGNED",
                    workload_delta=1,
                    work_week_start=week_start,
                    assigned_at=now,
                ),
            ]
        )
        session.commit()

        loads = employee_loads(session, "company", now, "Asia/Shanghai")
        assert {employee.display_name: count for employee, count in loads} == {
            "员工甲": 0,
            "员工乙": 1,
        }
        assert choose_employee(loads).display_name == "员工甲"
