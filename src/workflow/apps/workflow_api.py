from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from workflow import __version__
from workflow.config import get_settings
from workflow.db.models import (
    ActorProfile,
    Attachment,
    AttachmentStatus,
    AuditEvent,
    BackgroundJob,
    JobStatus,
    Role,
)
from workflow.db.session import create_engine_from_settings, make_session_factory, session_scope
from workflow.errors import (
    Forbidden,
    Unauthenticated,
    WorkflowError,
)
from workflow.idempotency import replay_or_none, save_response
from workflow.identity import find_actor_by_token, sync_actor_profiles
from workflow.logging import configure_logging
from workflow.probes.storage import ProbeStorage
from workflow.request_context import reset_request_id, set_request_id
from workflow.storage import LocalStorage
from workflow.t2.contracts import StructuredTopicImportBody
from workflow.t2.service import T2Service

configure_logging("workflow-api")
logger = logging.getLogger("workflow.api")

settings = get_settings()
engine = create_engine_from_settings(settings)
session_factory = make_session_factory(engine)
storage = LocalStorage(settings.file_data_dir, settings.disk_reject_percent)
probe_storage = ProbeStorage(settings.probe_data_dir, settings.public_base_url)
t2_service = T2Service(settings)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    settings.file_data_dir.mkdir(parents=True, exist_ok=True)
    with session_scope(session_factory) as session:
        sync_actor_profiles(session, settings)
    yield
    engine.dispose()


app = FastAPI(
    title="workflow-api",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class RequestLogMiddleware:
    """纯 ASGI 请求日志：关联 Nginx 的 X-Request-ID，记录耗时与状态码。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith("/health/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode("latin-1") or (
            f"req_{uuid.uuid4().hex}"
        )
        started = time.perf_counter()
        status_code = 0
        start_message: dict[str, Any] | None = None
        json_response = False
        body_parts: list[bytes] = []
        context_token = set_request_id(request_id)

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal json_response, start_message, status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    item for item in message.setdefault("headers", []) if item[0] != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
                json_response = any(
                    name == b"content-type" and value.startswith(b"application/json")
                    for name, value in headers
                )
                if json_response:
                    start_message = message
                    return
            elif message["type"] == "http.response.body" and json_response:
                body_parts.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = b"".join(body_parts)
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    payload.setdefault("request_id", request_id)
                    body = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                assert start_message is not None
                start_message["headers"] = [
                    item
                    for item in start_message["headers"]
                    if item[0] != b"content-length"
                ] + [(b"content-length", str(len(body)).encode("ascii"))]
                await send(start_message)
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(context_token)
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "result_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )


_REMEDIATIONS = {
    "UNAUTHENTICATED": "检查 Bearer Token 是否存在、有效且未被撤销。",
    "FORBIDDEN": "使用具备该工具权限的入口和人员 Token。",
    "RESOURCE_NOT_FOUND": "刷新列表并确认资源编号与当前公司或负责人一致。",
    "INVALID_ARGUMENT": "按工具参数说明修正输入后重试。",
    "INVALID_STATE_TRANSITION": "刷新任务详情，按 next_actions 中允许的动作继续。",
    "IDEMPOTENCY_CONFLICT": "为不同业务内容使用新的幂等键；原请求可继续使用原键。",
    "CONCURRENT_MODIFICATION": "刷新最新状态，确认后使用新的幂等键重试。",
    "EXTERNAL_DEPENDENCY_FAILED": "检查依赖服务健康状态后使用相同幂等键重试。",
}


@app.exception_handler(WorkflowError)
async def workflow_error_handler(_: Request, exc: WorkflowError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "remediation": _REMEDIATIONS.get(exc.code, "保留 request_id 并联系管理员。"),
            },
            "next_actions": [],
        },
    )


_HTTP_ERROR_CODES = {
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    405: "INVALID_ARGUMENT",
    410: "RESOURCE_NOT_FOUND",
    413: "FILE_TOO_LARGE",
    422: "INVALID_ARGUMENT",
    503: "EXTERNAL_DEPENDENCY_FAILED",
}


@app.exception_handler(HTTPException)
async def http_error_handler(_: Any, exc: HTTPException) -> JSONResponse:
    code = _HTTP_ERROR_CODES.get(
        exc.status_code, "INTERNAL_ERROR" if exc.status_code >= 500 else "INVALID_ARGUMENT"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": code,
                "message": str(exc.detail),
                "remediation": _REMEDIATIONS.get(code, "保留 request_id 并联系管理员。"),
            },
            "next_actions": [],
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_: Any, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "请求参数校验失败。",
                "remediation": "按工具参数 Schema 修正字段、类型和取值后重试。",
            },
            "next_actions": [],
        },
    )


@app.exception_handler(StaleDataError)
async def stale_data_handler(_: Request, __: StaleDataError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "error": {
                "code": "CONCURRENT_MODIFICATION",
                "message": "资源已被其他请求修改。",
                "remediation": _REMEDIATIONS["CONCURRENT_MODIFICATION"],
            },
            "next_actions": [],
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_api_error",
        extra={"path": request.url.path, "result_code": "INTERNAL_ERROR"},
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "系统处理失败。",
                "remediation": "保留 request_id 并联系管理员检查 API 日志。",
            },
            "next_actions": [],
        },
    )


def db_session() -> Iterator[Session]:
    with session_scope(session_factory) as session:
        yield session


def current_actor(
    session: Annotated[Session, Depends(db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> ActorProfile:
    if not authorization:
        raise Unauthenticated("缺少 Bearer Token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated("Bearer Token 格式错误")
    actor = find_actor_by_token(session, settings, token)
    if actor is None:
        raise Unauthenticated("Token 无效")
    return actor


def boss_actor(
    actor: Annotated[ActorProfile, Depends(current_actor)],
) -> ActorProfile:
    if actor.role != Role.BOSS:
        raise Forbidden("仅老板可执行此操作")
    return actor


def employee_actor(
    actor: Annotated[ActorProfile, Depends(current_actor)],
) -> ActorProfile:
    if actor.role != Role.EMPLOYEE:
        raise Forbidden("仅员工可执行此操作")
    return actor


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok", "service": "workflow-api", "phase": "T4"}


@app.get("/health/ready")
def health_ready(
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, object]:
    try:
        session.execute(text("SELECT 1"))
        free_percent = round(storage.free_percent(), 2)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="数据库或附件目录不可用") from exc
    return {
        "status": "ready",
        "service": "workflow-api",
        "phase": "T4",
        "database_ready": True,
        "file_storage_ready": True,
        "disk_free_percent": free_percent,
        "disk_warning": free_percent < settings.disk_warn_percent,
    }


@app.get("/internal/t1/identity")
def identity(
    actor: Annotated[ActorProfile, Depends(current_actor)],
) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "actor_id": actor.id,
            "display_name": actor.display_name,
            "role": actor.role.value,
            "company_id": actor.company_id,
        },
    }


class IdempotencyProbe(BaseModel):
    idempotency_key: str
    value: str


@app.post("/internal/t1/idempotency-probe")
def idempotency_probe(
    body: IdempotencyProbe,
    actor: Annotated[ActorProfile, Depends(current_actor)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    payload = {"value": body.value}
    replay = replay_or_none(
        session,
        company_id=actor.company_id,
        actor_id=actor.id,
        tool="t1_idempotency_probe",
        key=body.idempotency_key,
        payload=payload,
    )
    if replay is not None:
        return {**replay, "deduplicated": True}
    response = {"success": True, "nonce": secrets.token_hex(8), "deduplicated": False}
    replayed = save_response(
        session,
        company_id=actor.company_id,
        actor_id=actor.id,
        tool="t1_idempotency_probe",
        key=body.idempotency_key,
        payload=payload,
        response=response,
    )
    if replayed is not None:
        return {**replayed, "deduplicated": True}
    return response


@app.post("/internal/t1/jobs/noop")
def create_noop_job(
    actor: Annotated[ActorProfile, Depends(boss_actor)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    job = BackgroundJob(
        company_id=actor.company_id,
        job_type="NOOP",
        status=JobStatus.PENDING,
        max_attempts=settings.worker_max_attempts,
    )
    session.add(job)
    session.flush()
    session.add(
        AuditEvent(
            company_id=actor.company_id,
            actor_id=actor.id,
            action="T1_NOOP_JOB_CREATED",
            object_type="background_job",
            object_id=job.id,
            request_id=secrets.token_hex(16),
            after_state={"job_type": "NOOP", "status": "PENDING"},
        )
    )
    return {"success": True, "data": {"job_id": job.id}}


@app.get("/internal/t1/status")
def t1_status(
    _: Annotated[ActorProfile, Depends(boss_actor)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    counts = {
        "actors": session.scalar(select(func.count()).select_from(ActorProfile)) or 0,
        "jobs": session.scalar(select(func.count()).select_from(BackgroundJob)) or 0,
        "audit_events": session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
    }
    return {"success": True, "data": counts}


@app.get("/files/{opaque_file_id}")
def download_file(
    opaque_file_id: str,
    session: Annotated[Session, Depends(db_session)],
) -> FileResponse:
    attachment = session.scalar(
        select(Attachment).where(Attachment.opaque_file_id == opaque_file_id)
    )
    if attachment is not None:
        if attachment.status != AttachmentStatus.READY:
            raise HTTPException(status_code=410, detail="文件尚未就绪或已不可用")
        try:
            path = storage.path_for(attachment.storage_key)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail="文件记录存在，但内容已不可用") from exc
        return FileResponse(
            path=path,
            media_type=attachment.mime_type,
            filename=attachment.original_filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )
    try:
        metadata = probe_storage.load_metadata(opaque_file_id)
        path = probe_storage.file_path(opaque_file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    return FileResponse(
        path=path,
        media_type=metadata.mime_type,
        filename=metadata.original_filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# 内部业务工具端点（规格 §3.1：MCP 不直连数据库，所有写操作进入工作流 API，
# 由 API 第二层校验 Token 身份、角色、公司和资源归属）。
# 这些路由不经 Nginx 对公网开放，仅供 boss-mcp / employee-mcp 容器内调用。
# ---------------------------------------------------------------------------


class ImportTopicBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_filename: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    file_key: str | None = Field(default=None, min_length=1, max_length=64)
    file_url: str | None = None


class BatchBody(BaseModel):
    import_batch_id: str


class CancelTaskBody(BaseModel):
    task_no: str
    idempotency_key: str
    reason: str | None = None


class ReassignBody(BaseModel):
    task_no: str
    new_employee_id: str
    idempotency_key: str
    reason: str | None = None


class PriorityBody(BaseModel):
    task_no: str
    priority: bool
    idempotency_key: str


class ListProjectsBody(BaseModel):
    status: str | None = None
    assignee_id: str | None = None
    priority: bool | None = None
    import_batch_id: str | None = None
    keyword: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    # T2 旧契约兼容；新调用使用 page_size。
    limit: int | None = Field(default=None, ge=1, le=100)


class TaskNoBody(BaseModel):
    task_no: str


class ReviewBody(BaseModel):
    task_no: str
    decision: str
    idempotency_key: str
    comment: str | None = None
    reason_category: str | None = None


class MyTasksBody(BaseModel):
    status: str | None = None
    priority: bool | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    limit: int | None = Field(default=None, ge=1, le=100)


class SubmitScriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_no: str
    original_filename: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    file_key: str | None = Field(default=None, min_length=1, max_length=64)
    file_url: str | None = None
    note: str | None = None


class UploadFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_base64: str = Field(min_length=1)


class PageBody(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


class OperationalIssuesBody(PageBody):
    issue_type: str | None = None
    status: str | None = None


class ReportBlockerBody(BaseModel):
    task_no: str
    blocker_type: str
    description: str
    idempotency_key: str


@app.post("/internal/tools/upload-file")
def tool_upload_file(
    body: UploadFileBody,
    actor: Annotated[ActorProfile, Depends(current_actor)],
) -> dict[str, object]:
    return t2_service.upload_file(
        actor_name=actor.display_name,
        role=actor.role,
        file_base64=body.file_base64,
    )


@app.post("/internal/tools/import-topic-document")
def tool_import_topic_document(
    body: ImportTopicBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.import_topics(
        actor_name=actor.display_name,
        original_filename=body.original_filename,
        idempotency_key=body.idempotency_key,
        file_key=body.file_key,
        file_url=body.file_url,
    )


@app.post("/internal/tools/import-structured-topics")
def tool_import_structured_topics(
    body: StructuredTopicImportBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.import_structured_topics(
        actor_name=actor.display_name,
        original_filename=body.original_filename,
        idempotency_key=body.idempotency_key,
        topics=body.topics,
        warnings=body.warnings,
        schema_version=body.schema_version,
        file_key=body.file_key,
        file_url=body.file_url,
    )


@app.post("/internal/tools/list-import-batch-tasks")
def tool_list_import_batch_tasks(
    body: BatchBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.list_batch(
        actor_name=actor.display_name, import_batch_id=body.import_batch_id
    )


@app.post("/internal/tools/delete-imported-task")
def tool_delete_imported_task(
    body: CancelTaskBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.cancel_imported_task(
        actor_name=actor.display_name,
        task_no=body.task_no,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
    )


@app.post("/internal/tools/change-task-assignee")
def tool_change_task_assignee(
    body: ReassignBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.reassign(
        actor_name=actor.display_name,
        task_no=body.task_no,
        new_employee_id=body.new_employee_id,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
    )


@app.post("/internal/tools/set-task-priority")
def tool_set_task_priority(
    body: PriorityBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.set_priority(
        actor_name=actor.display_name,
        task_no=body.task_no,
        priority=body.priority,
        idempotency_key=body.idempotency_key,
    )


@app.post("/internal/tools/list-employees")
def tool_list_employees(
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.list_employees(actor_name=actor.display_name)


@app.post("/internal/tools/list-content-projects")
def tool_list_content_projects(
    body: ListProjectsBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.list_projects(
        actor_name=actor.display_name,
        status=body.status,
        assignee_id=body.assignee_id,
        priority=body.priority,
        import_batch_id=body.import_batch_id,
        keyword=body.keyword,
        created_from=body.created_from,
        created_to=body.created_to,
        page=body.page,
        page_size=body.limit or body.page_size,
    )


@app.post("/internal/tools/get-content-project")
def tool_get_content_project(
    body: TaskNoBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.get_project(actor_name=actor.display_name, task_no=body.task_no)


@app.post("/internal/tools/list-pending-reviews")
def tool_list_pending_reviews(
    body: PageBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.pending_reviews(
        actor_name=actor.display_name,
        page=body.page,
        page_size=body.page_size,
    )


@app.post("/internal/tools/get-workflow-dashboard")
def tool_get_workflow_dashboard(
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.dashboard(actor_name=actor.display_name)


@app.post("/internal/tools/list-operational-issues")
def tool_list_operational_issues(
    body: OperationalIssuesBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.operational_issues(
        actor_name=actor.display_name,
        issue_type=body.issue_type,
        status=body.status,
        page=body.page,
        page_size=body.page_size,
    )


@app.post("/internal/tools/review-script-submission")
def tool_review_script_submission(
    body: ReviewBody,
    actor: Annotated[ActorProfile, Depends(boss_actor)],
) -> dict[str, Any]:
    return t2_service.review_script(
        actor_name=actor.display_name,
        task_no=body.task_no,
        decision=body.decision,
        comment=body.comment,
        reason_category=body.reason_category,
        idempotency_key=body.idempotency_key,
    )


@app.post("/internal/tools/list-my-tasks")
def tool_list_my_tasks(
    body: MyTasksBody,
    actor: Annotated[ActorProfile, Depends(employee_actor)],
) -> dict[str, Any]:
    return t2_service.list_my_tasks(
        actor_name=actor.display_name,
        status=body.status,
        priority=body.priority,
        page=body.page,
        page_size=body.limit or body.page_size,
    )


@app.post("/internal/tools/get-my-task")
def tool_get_my_task(
    body: TaskNoBody,
    actor: Annotated[ActorProfile, Depends(employee_actor)],
) -> dict[str, Any]:
    return t2_service.get_my_task(actor_name=actor.display_name, task_no=body.task_no)


@app.post("/internal/tools/submit-script-file")
def tool_submit_script_file(
    body: SubmitScriptBody,
    actor: Annotated[ActorProfile, Depends(employee_actor)],
) -> dict[str, Any]:
    return t2_service.submit_script(
        actor_name=actor.display_name,
        task_no=body.task_no,
        original_filename=body.original_filename,
        idempotency_key=body.idempotency_key,
        file_key=body.file_key,
        file_url=body.file_url,
        note=body.note,
    )


@app.post("/internal/tools/report-task-blocker")
def tool_report_task_blocker(
    body: ReportBlockerBody,
    actor: Annotated[ActorProfile, Depends(employee_actor)],
) -> dict[str, Any]:
    return t2_service.report_blocker(
        actor_name=actor.display_name,
        task_no=body.task_no,
        blocker_type=body.blocker_type,
        description=body.description,
        idempotency_key=body.idempotency_key,
    )


app = RequestLogMiddleware(app)
