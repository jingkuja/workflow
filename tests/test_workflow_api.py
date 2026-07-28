"""内部工作流 API 端到端测试（规格 §3.1 权限双层校验的 API 层）。

通过 FastAPI TestClient 走真实 HTTP 路由：Token → 身份 → 角色 → 业务服务。
数据库使用临时 SQLite 文件，文件存储使用临时目录。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BOSS = {"Authorization": "Bearer boss-token-at-least-16"}
EMP_A = {"Authorization": "Bearer employee-a-token-12345"}
EMP_B = {"Authorization": "Bearer employee-b-token-12345"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'api.sqlite'}")
    monkeypatch.setenv("FILE_DATA_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("PROBE_DATA_DIR", str(tmp_path / "probes"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("COMPANY_ID", "company-api")
    monkeypatch.setenv("MCP_BOSS_NAME", "老板测试")
    monkeypatch.setenv("MCP_BOSS_TOKEN", "boss-token-at-least-16")
    monkeypatch.setenv(
        "MCP_EMPLOYEES_JSON",
        '[{"name":"员工甲","token":"employee-a-token-12345","wecom_userid":"","active":true},'
        '{"name":"员工乙","token":"employee-b-token-12345","wecom_userid":"","active":true}]',
    )
    from workflow.config import get_settings

    get_settings.cache_clear()
    import workflow.apps.workflow_api as module
    from workflow.db.models import Base

    Base.metadata.create_all(module.engine)
    with TestClient(module.app) as client:
        yield client
    get_settings.cache_clear()


def test_auth_matrix_and_unified_error_structure(api) -> None:
    ok = api.get("/internal/t1/identity", headers=BOSS)
    assert ok.status_code == 200
    assert ok.json()["data"]["role"] == "BOSS"

    missing = api.get("/internal/t1/identity")
    assert missing.status_code == 401
    assert missing.json()["success"] is False
    assert missing.json()["error"]["code"] == "UNAUTHENTICATED"

    invalid = api.get("/internal/t1/identity", headers={"Authorization": "Bearer nope-nope-nope"})
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "UNAUTHENTICATED"

    # 员工调用老板端点 → 403 FORBIDDEN；老板调用员工端点同样拒绝。
    forbidden = api.post("/internal/tools/list-employees", headers=EMP_A, json={})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    boss_on_employee = api.post("/internal/tools/list-my-tasks", headers=BOSS, json={})
    assert boss_on_employee.status_code == 403
    assert boss_on_employee.json()["error"]["code"] == "FORBIDDEN"

    bad_params = api.post("/internal/tools/import-topic-document", headers=BOSS, json={})
    assert bad_params.status_code == 400
    assert bad_params.json()["error"]["code"] == "INVALID_ARGUMENT"

    not_found = api.get("/files/does-not-exist-at-all")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_full_topic_script_flow_through_internal_api(api) -> None:
    document = Path("docs/AI行业选题文档上传样例.docx").read_bytes()
    imported = api.post(
        "/internal/tools/import-topic-document",
        headers=BOSS,
        json={
            "original_filename": "选题.docx",
            "idempotency_key": "api-import-0001",
            "content_base64": base64.b64encode(document).decode(),
        },
    )
    assert imported.status_code == 200
    data = imported.json()["data"]
    assert data["created_count"] == 10
    assert data["parse_status"] == "COMPLETED"

    task = data["tasks"][0]
    owner_headers = EMP_A if task["assigned_employee_name"] == "员工甲" else EMP_B
    other_headers = EMP_B if owner_headers is EMP_A else EMP_A

    # 他人任务对员工不可见。
    invisible = api.post(
        "/internal/tools/get-my-task", headers=other_headers, json={"task_no": task["task_no"]}
    )
    assert invisible.status_code == 404
    assert invisible.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    mine = api.post("/internal/tools/list-my-tasks", headers=owner_headers, json={})
    assert any(item["task_no"] == task["task_no"] for item in mine.json()["data"])

    submitted = api.post(
        "/internal/tools/submit-script-file",
        headers=owner_headers,
        json={
            "task_no": task["task_no"],
            "original_filename": "演播稿.txt",
            "idempotency_key": "api-submit-0001",
            "content_base64": base64.b64encode("第一版".encode()).decode(),
            "note": "初稿",
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["data"]["version_no"] == 1

    pending = api.post("/internal/tools/list-pending-reviews", headers=BOSS, json={})
    assert any(item["task_no"] == task["task_no"] for item in pending.json()["data"])

    reviewed = api.post(
        "/internal/tools/review-script-submission",
        headers=BOSS,
        json={
            "task_no": task["task_no"],
            "decision": "APPROVED",
            "idempotency_key": "api-review-0001",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["project_status"] == "WAITING_FOR_FILMING"

    # 相同幂等键重放返回首次结果，不产生重复审核。
    replay = api.post(
        "/internal/tools/review-script-submission",
        headers=BOSS,
        json={
            "task_no": task["task_no"],
            "decision": "APPROVED",
            "idempotency_key": "api-review-0001",
        },
    )
    assert replay.status_code == 200
    assert replay.json() == reviewed.json()
