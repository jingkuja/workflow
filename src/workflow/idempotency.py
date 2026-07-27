from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow.db.models import IdempotencyRecord
from workflow.errors import IdempotencyConflict


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.company_id == company_id,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.tool == tool,
            IdempotencyRecord.idempotency_key == key,
        )
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
) -> IdempotencyRecord:
    record = IdempotencyRecord(
        company_id=company_id,
        actor_id=actor_id,
        tool=tool,
        idempotency_key=key,
        request_fingerprint=request_fingerprint(payload),
        response_json=response,
    )
    session.add(record)
    session.flush()
    return record
