from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pydantic import SecretStr

from workflow.config import Settings
from workflow.db.models import Base
from workflow.db.session import make_session_factory, session_scope
from workflow.errors import NotFound
from workflow.identity import sync_actor_profiles
from workflow.t2.service import T2Service


def make_service(tmp_path: Path) -> T2Service:
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
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-a-token-12345","active":true},'
            '{"name":"员工乙","token":"employee-b-token-12345","active":true}]'
        ),
    )
    service = T2Service(settings)
    Base.metadata.create_all(service.engine)
    factory = make_session_factory(service.engine)
    with session_scope(factory) as session:
        sync_actor_profiles(session, settings)
    return service


def test_t2_full_topic_script_workflow(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document = Path("docs/AI行业选题文档上传样例.docx").read_bytes()
    encoded = base64.b64encode(document).decode()

    imported = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="import-key-0001",
        content_base64=encoded,
        file_url=None,
    )
    tasks = imported["data"]["tasks"]
    assert len(tasks) == 10
    names = [task["assigned_employee_name"] for task in tasks]
    assert abs(names.count("员工甲") - names.count("员工乙")) <= 1

    duplicate = service.import_topics(
        actor_name="老板测试",
        original_filename="选题.docx",
        idempotency_key="import-key-0002",
        content_base64=encoded,
        file_url=None,
    )
    assert duplicate["data"]["deduplicated"] is True
    assert duplicate["data"]["created_count"] == 0

    target = tasks[0]
    owner = target["assigned_employee_name"]
    other = "员工乙" if owner == "员工甲" else "员工甲"
    with pytest.raises(NotFound):
        service.get_my_task(actor_name=other, task_no=target["task_no"])

    submitted = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿.txt",
        idempotency_key="submit-key-0001",
        content_base64=base64.b64encode("第一版".encode()).decode(),
        file_url=None,
        note="初稿",
    )
    replay = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿.txt",
        idempotency_key="submit-key-0001",
        content_base64=base64.b64encode("第一版".encode()).decode(),
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
    second = service.submit_script(
        actor_name=owner,
        task_no=target["task_no"],
        original_filename="演播稿-v2.md",
        idempotency_key="submit-key-0002",
        content_base64=base64.b64encode("第二版".encode()).decode(),
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
