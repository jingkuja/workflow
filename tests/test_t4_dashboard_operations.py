from __future__ import annotations

import base64
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import func, select

from workflow.config import Settings
from workflow.db.models import (
    BackgroundJob,
    Base,
    JobStatus,
    Notification,
    NotificationStatus,
)
from workflow.db.session import make_session_factory, session_scope
from workflow.identity import sync_actor_profiles
from workflow.t2.service import T2Service


def make_service(tmp_path: Path) -> T2Service:
    settings = Settings(
        _env_file=None,
        app_env="test",
        company_id="company-t4",
        database_url=f"sqlite+pysqlite:///{tmp_path / 't4.sqlite'}",
        file_data_dir=tmp_path / "files",
        probe_data_dir=tmp_path / "probes",
        public_base_url="http://testserver",
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-a-token-12345",'
            '"wecom_userid":"userid-a","active":true},'
            '{"name":"员工乙","token":"employee-b-token-12345",'
            '"wecom_userid":"","active":true}]'
        ),
    )
    service = T2Service(settings)
    Base.metadata.create_all(service.engine)
    factory = make_session_factory(service.engine)
    with session_scope(factory) as session:
        sync_actor_profiles(session, settings)
    return service


def import_sample(service: T2Service) -> list[dict[str, object]]:
    document = Path("docs/AI行业选题文档上传样例.docx").read_bytes()
    result = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="t4-import-0001",
        content_base64=base64.b64encode(document).decode(),
        file_url=None,
    )
    return result["data"]["tasks"]


def test_t4_dashboard_pagination_timeline_and_terminal_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    tasks = import_sample(service)

    first_page = service.list_projects(
        actor_name="老板测试", page=1, page_size=3
    )
    assert len(first_page["data"]) == 3
    assert first_page["pagination"] == {
        "page": 1,
        "page_size": 3,
        "total_items": 10,
        "total_pages": 4,
        "has_previous": False,
        "has_next": True,
    }
    assert "next_page" in first_page["next_actions"]

    task = tasks[0]
    service.set_priority(
        actor_name="老板测试",
        task_no=str(task["task_no"]),
        priority=True,
        idempotency_key="t4-priority-0001",
    )
    with session_scope(service.sessions) as session:
        assert session.scalar(select(func.count()).select_from(Notification)) == 11

    service.submit_script(
        actor_name=str(task["assigned_employee_name"]),
        task_no=str(task["task_no"]),
        original_filename="演播稿.txt",
        idempotency_key="t4-submit-0001",
        content_base64=base64.b64encode("演播稿".encode()).decode(),
        file_url=None,
        note="T4",
    )
    pending = service.pending_reviews(actor_name="老板测试", page_size=1)
    assert pending["pagination"]["total_items"] == 1
    assert pending["data"][0]["latest_submission"]["download_url"].startswith(
        "http://testserver/files/"
    )
    service.review_script(
        actor_name="老板测试",
        task_no=str(task["task_no"]),
        decision="APPROVED",
        comment="通过",
        reason_category=None,
        idempotency_key="t4-review-0001",
    )

    detail = service.get_project(
        actor_name="老板测试", task_no=str(task["task_no"])
    )["data"]
    assert detail["project_status"] == "WAITING_FOR_FILMING"
    assert detail["task"]["next_actions"] == []
    assert {event["action"] for event in detail["timeline"]} >= {
        "SCRIPT_TASK_CREATED",
        "SCRIPT_SUBMITTED",
        "SCRIPT_APPROVED",
    }

    dashboard = service.dashboard(actor_name="老板测试")["data"]
    assert dashboard["normal_terminal_status"] == "WAITING_FOR_FILMING"
    assert dashboard["stage_counts"]["WAITING_FOR_FILMING"] == 1
    assert dashboard["this_week"]["target_min"] == 35
    assert dashboard["this_week"]["gap_to_min"] >= 34
    assert len(dashboard["employee_loads"]) == 2


def test_t4_blockers_and_failed_operations_are_queryable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task = import_sample(service)[0]
    reported = service.report_blocker(
        actor_name=str(task["assigned_employee_name"]),
        task_no=str(task["task_no"]),
        blocker_type="MATERIAL_MISSING",
        description="缺少背景材料",
        idempotency_key="t4-blocker-0001",
    )
    replay = service.report_blocker(
        actor_name=str(task["assigned_employee_name"]),
        task_no=str(task["task_no"]),
        blocker_type="MATERIAL_MISSING",
        description="缺少背景材料",
        idempotency_key="t4-blocker-0001",
    )
    assert replay == reported

    with session_scope(service.sessions) as session:
        session.add(
            BackgroundJob(
                company_id="company-t4",
                job_type="TEST_FAILURE",
                status=JobStatus.DEAD,
                attempts=5,
                max_attempts=5,
                last_error="测试失败",
            )
        )
        notification = session.scalar(
            select(Notification).where(Notification.company_id == "company-t4")
        )
        assert notification is not None
        notification.status = NotificationStatus.DEAD
        notification.attempts = 5
        notification.response_summary = "通知失败"

    issues = service.operational_issues(actor_name="老板测试", page_size=10)
    assert {item["issue_type"] for item in issues["data"]} == {
        "BACKGROUND_JOB",
        "NOTIFICATION",
        "BLOCKER",
    }
    dashboard = service.dashboard(actor_name="老板测试")["data"]
    assert dashboard["open_blocker_count"] == 1
    assert dashboard["failed_background_job_count"] == 1
    assert dashboard["failed_notification_count"] == 1
