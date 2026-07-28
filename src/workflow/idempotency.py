from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from workflow.db.models import IdempotencyRecord
from workflow.errors import IdempotencyConflict


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_record(
    session: Session,
    *,
    company_id: str,
    actor_id: str,
    tool: str,
    key: str,
) -> IdempotencyRecord | None:
    return session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.company_id == company_id,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.tool == tool,
            IdempotencyRecord.idempotency_key == key,
        )
    )


def replay_or_none(
    session: Session,
    *,
    company_id: str,
    actor_id: str,
    tool: str,
    key: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    fingerprint = request_fingerprint(payload)
    record = _load_record(
        session, company_id=company_id, actor_id=actor_id, tool=tool, key=key
    )
    if record is None:
        return None
    if record.request_fingerprint != fingerprint:
        raise IdempotencyConflict("同一幂等键已用于不同请求。")
    return record.response_json


def save_response(
    session: Session,
    *,
    company_id: str,
    actor_id: str,
    tool: str,
    key: str,
    payload: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any] | None:
    """保存首次响应；并发下唯一约束冲突时回退为重放首次响应。

    返回 None 表示本次保存成功；返回字典表示另一个并发请求已抢先提交，
    调用方应丢弃本次计算结果、改为返回该首次响应。指纹不一致仍抛
    IdempotencyConflict。
    """
    fingerprint = request_fingerprint(payload)
    record = IdempotencyRecord(
        company_id=company_id,
        actor_id=actor_id,
        tool=tool,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        response_json=response,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        existing = _load_record(
            session, company_id=company_id, actor_id=actor_id, tool=tool, key=key
        )
        if existing is None:
            raise IdempotencyConflict("幂等记录冲突且无法读取首次响应。") from None
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict("同一幂等键已用于不同请求。") from None
        return existing.response_json
    return None
