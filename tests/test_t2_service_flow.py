from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from workflow.config import Role, Settings
from workflow.db.models import Base, McpFileUpload
from workflow.db.session import make_session_factory, session_scope
from workflow.errors import ResourceNotFound
from workflow.identity import sync_actor_profiles
from workflow.t2.service import T2Service

_TWO_EMPLOYEES = (
    '[{"name":"员工甲","token":"employee-a-token-12345","active":true},'
    '{"name":"员工乙","token":"employee-b-token-12345","active":true}]'
)


def make_service(tmp_path: Path, employees_json: str = _TWO_EMPLOYEES) -> T2Service:
    database_path = tmp_path / "t2.sqlite"
    settings = Settings(
        _env_file=None,
        app_env="test",
        company_id="company-t2",
        database_url=f"sqlite+pysqlite:///{database_path}",
        file_data_dir=tmp_path / "files",
        probe_data_dir=tmp_path / "probes",
        public_base_url="http://testserver",
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_employees_json=employees_json,
    )
    service = T2Service(settings)
    Base.metadata.create_all(service.engine)
    factory = make_session_factory(service.engine)
    with session_scope(factory) as session:
        sync_actor_profiles(session, settings)
    return service


def upload_file(
    service: T2Service,
    *,
    actor_name: str,
    role: Role,
    content: bytes,
) -> str:
    response = service.upload_file(
        actor_name=actor_name,
        role=role,
        file_base64=base64.b64encode(content).decode(),
    )
    return str(response["data"]["file_key"])


def test_uploaded_file_key_is_bound_to_actor_and_expiry(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    file_key = upload_file(
        service,
        actor_name="员工甲",
        role="EMPLOYEE",
        content="演播稿".encode(),
    )

    with pytest.raises(ResourceNotFound, match="不属于当前调用人"):
        service._receive_document(
            actor_name="员工乙",
            role="EMPLOYEE",
            filename="演播稿.txt",
            allowed={".txt"},
            max_bytes=1024,
            file_key=file_key,
            file_url=None,
        )

    with session_scope(service.sessions) as session:
        upload = session.scalar(select(McpFileUpload).where(McpFileUpload.file_key == file_key))
        assert upload is not None
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ResourceNotFound, match="已过期"):
        service._receive_document(
            actor_name="员工甲",
            role="EMPLOYEE",
            filename="演播稿.txt",
            allowed={".txt"},
            max_bytes=1024,
            file_key=file_key,
            file_url=None,
        )


def test_t2_full_topic_script_workflow(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document = Path("docs/AI行业选题文档上传样例.docx").read_bytes()
    topic_file_key = upload_file(
        service, actor_name="老板测试", role="BOSS", content=document
    )

    imported = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="import-key-0001",
        file_key=topic_file_key,
        file_url=None,
    )
    tasks = imported["data"]["tasks"]
    assert len(tasks) == 10
    assert imported["data"]["parse_status"] == "COMPLETED"
    assert imported["data"]["failures"] == []
    assert imported["data"]["pending_assignment_count"] == 0
    names = [task["assigned_employee_name"] for task in tasks]
    assert abs(names.count("员工甲") - names.count("员工乙")) <= 1

    duplicate = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="import-key-0002",
        file_key=topic_file_key,
        file_url=None,
    )
    assert duplicate["data"]["deduplicated"] is True
    assert duplicate["data"]["created_count"] == 0

    target = tasks[0]
    owner = target["assigned_employee_name"]
    other = "员工乙" if owner == "员工甲" else "员工甲"
    with pytest.raises(ResourceNotFound):
        service.get_my_task(actor_name=other, task_no=target["task_no"])

    first_script_key = upload_file(
        service,
        actor_name=owner,
        role="EMPLOYEE",
        content="第一版".encode(),
    )
    submitted = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿.txt",
        idempotency_key="submit-key-0001",
        file_key=first_script_key,
        file_url=None,
        note="初稿",
    )
    replay = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿.txt",
        idempotency_key="submit-key-0001",
        file_key=first_script_key,
        file_url=None,
        note="初稿",
    )
    assert submitted == replay
    rejected = service.review_script(
        actor_name="老板测试",
        task_no=target["task_no"],
        decision="REJECTED",
        comment="修改开场",
        reason_category="OPENING_HOOK",
        idempotency_key="review-key-0001",
    )
    assert rejected["data"]["task_status"] == "REJECTED"
    second_script_key = upload_file(
        service,
        actor_name=owner,
        role="EMPLOYEE",
        content="第二版".encode(),
    )
    second = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿-v2.md",
        idempotency_key="submit-key-0002",
        file_key=second_script_key,
        file_url=None,
        note="修改稿",
    )
    assert second["data"]["version_no"] == 2
    approved = service.review_script(
        actor_name="老板测试",
        task_no=target["task_no"],
        decision="APPROVED",
        comment="通过",
        reason_category=None,
        idempotency_key="review-key-0002",
    )
    assert approved["data"]["project_status"] == "WAITING_FOR_FILMING"

    adjustable = tasks[1]
    service.set_priority(
        actor_name="老板测试",
        task_no=adjustable["task_no"],
        priority=True,
        idempotency_key="priority-key-01",
    )
    employees = service.list_employees(actor_name="老板测试")["data"]
    new_employee_id = next(
        item["employee_id"]
        for item in employees
        if item["display_name"] != adjustable["assigned_employee_name"]
    )
    reassigned = service.reassign(
        actor_name="老板测试",
        task_no=adjustable["task_no"],
        new_employee_id=new_employee_id,
        reason="均衡测试",
        idempotency_key="reassign-key-01",
    )
    current = next(
        item for item in reassigned["data"]["tasks"] if item["task_no"] == adjustable["task_no"]
    )
    assert current["assigned_employee_id"] == new_employee_id

    cancelled = service.cancel_imported_task(
        actor_name="老板测试",
        task_no=tasks[2]["task_no"],
        reason="取消测试",
        idempotency_key="cancel-key-0001",
    )
    cancelled_task = next(
        item for item in cancelled["data"]["tasks"] if item["task_no"] == tasks[2]["task_no"]
    )
    assert cancelled_task["status"] == "CANCELLED"


def test_import_without_employees_creates_pending_assignment_tasks(tmp_path: Path) -> None:
    service = make_service(tmp_path, employees_json="[]")
    document = Path("docs/AI行业选题文档上传样例.docx").read_bytes()
    topic_file_key = upload_file(
        service, actor_name="老板测试", role="BOSS", content=document
    )

    imported = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="import-key-no-employee",
        file_key=topic_file_key,
        file_url=None,
    )

    data = imported["data"]
    assert data["created_count"] == 10
    assert data["pending_assignment_count"] == 10
    assert data["parse_status"] == "COMPLETED"
    assert all(task["status"] == "PENDING_ASSIGNMENT" for task in data["tasks"])
    assert all(task["assigned_employee_id"] is None for task in data["tasks"])

    cancelled = service.cancel_imported_task(
        actor_name="老板测试",
        task_no=data["tasks"][0]["task_no"],
        reason="无员工时取消",
        idempotency_key="cancel-pending-01",
    )
    cancelled_task = next(
        item
        for item in cancelled["data"]["tasks"]
        if item["task_no"] == data["tasks"][0]["task_no"]
    )
    assert cancelled_task["status"] == "CANCELLED"


def test_reassign_counts_new_employee_in_effective_week(tmp_path: Path) -> None:
    """改派事件的周归属必须与原始分配一致（effective_started_at 所在周）。

    直接构造下周一生效的任务，保证当前周与生效周不同，回归 P2-1。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from workflow.db.models import ActorProfile, ContentProject, StageTask, TaskAssignment
    from workflow.t2.calendar import week_start_for

    service = make_service(tmp_path)
    now = datetime.now(UTC)
    days_until_monday = (7 - now.weekday()) % 7 or 7
    effective = (now + timedelta(days=days_until_monday)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if week_start_for(effective, "Asia/Shanghai") == week_start_for(now, "Asia/Shanghai"):
        effective += timedelta(days=7)

    with session_scope(service.sessions) as session:
        employees = {
            profile.display_name: profile
            for profile in session.scalars(select(ActorProfile)).all()
        }
        project = ContentProject(
            company_id="company-t2", title="跨周项目", status="SCRIPT_IN_PROGRESS"
        )
        session.add(project)
        session.flush()
        task = StageTask(
            company_id="company-t2",
            project_id=project.id,
            task_no="WJ-20990101-0001",
            task_type="SCRIPT",
            status="IN_PROGRESS",
            assignee_id=employees["员工甲"].id,
            effective_started_at=effective,
        )
        session.add(task)
        session.flush()
        session.add(
            TaskAssignment(
                company_id="company-t2",
                task_id=task.id,
                assignee_id=employees["员工甲"].id,
                event_type="AUTO_ASSIGNED",
                workload_delta=1,
                work_week_start=week_start_for(effective, "Asia/Shanghai"),
                assigned_at=now,
            )
        )

    service.reassign(
        actor_name="老板测试",
        task_no="WJ-20990101-0001",
        new_employee_id=employees["员工乙"].id,
        reason="跨周改派",
        idempotency_key="reassign-week-01",
    )

    with session_scope(service.sessions) as session:
        events = session.scalars(
            select(TaskAssignment).where(TaskAssignment.task_id == task.id)
        ).all()
        effective_week = week_start_for(effective, "Asia/Shanghai")
        current_week = week_start_for(now, "Asia/Shanghai")
        assert effective_week != current_week
        by_type = {event.event_type: event for event in events}
        assert by_type["AUTO_ASSIGNED"].work_week_start == effective_week
        assert by_type["REASSIGN_REVERSED"].work_week_start == effective_week
        assert by_type["MANUAL_REASSIGNED"].work_week_start == effective_week
