from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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
from workflow.errors import WorkflowError
from workflow.idempotency import replay_or_none, save_response
from workflow.identity import find_actor_by_token, sync_actor_profiles
from workflow.probes.storage import ProbeStorage
from workflow.storage import LocalStorage

settings = get_settings()
engine = create_engine_from_settings(settings)
session_factory = make_session_factory(engine)
storage = LocalStorage(settings.file_data_dir, settings.disk_reject_percent)
probe_storage = ProbeStorage(settings.probe_data_dir, settings.public_base_url)


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


@app.exception_handler(WorkflowError)
async def workflow_error_handler(_: Any, exc: WorkflowError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": {"code": exc.code, "message": exc.message}},
    )


def db_session() -> Iterator[Session]:
    with session_scope(session_factory) as session:
        yield session


def current_actor(
    session: Annotated[Session, Depends(db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> ActorProfile:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer Token 格式错误")
    actor = find_actor_by_token(session, settings, token)
    if actor is None:
        raise HTTPException(status_code=401, detail="Token 无效")
    return actor


def boss_actor(
    actor: Annotated[ActorProfile, Depends(current_actor)],
) -> ActorProfile:
    if actor.role != Role.BOSS:
        raise HTTPException(status_code=403, detail="仅老板可执行此操作")
    return actor


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok", "service": "workflow-api", "phase": "T1"}


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
        "phase": "T1",
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
    save_response(
        session,
        company_id=actor.company_id,
        actor_id=actor.id,
        tool="t1_idempotency_probe",
        key=body.idempotency_key,
        payload=payload,
        response=response,
    )
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
