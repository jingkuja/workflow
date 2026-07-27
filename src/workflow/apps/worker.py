from __future__ import annotations

import logging
import os
import socket
import time

import httpx
from sqlalchemy.orm import Session

from workflow.config import get_settings
from workflow.db.session import create_engine_from_settings, make_session_factory, session_scope
from workflow.identity import sync_actor_profiles
from workflow.jobs import claim_job, fail_job, succeed_job
from workflow.notifications import (
    claim_notification,
    mark_notification_failed,
    mark_notification_sent,
)

logging.basicConfig(
    level=logging.INFO,
    format='{"level":"%(levelname)s","service":"workflow-worker","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)


def process_job(session: Session, worker_id: str) -> bool:
    settings = get_settings()
    job = claim_job(session, worker_id=worker_id, lease_seconds=settings.worker_lease_seconds)
    if job is None:
        return False
    try:
        if job.job_type == "NOOP":
            succeed_job(job)
        else:
            fail_job(job, f"未知任务类型: {job.job_type}", permanent=True)
    except Exception as exc:
        fail_job(job, str(exc))
        logger.exception("job_failed id=%s type=%s", job.id, job.job_type)
    return True


def process_notification(session: Session) -> bool:
    settings = get_settings()
    if not settings.notification_send_enabled:
        return False
    item = claim_notification(session)
    if item is None:
        return False
    webhook = settings.wecom_group_webhook_url
    if webhook is None:
        mark_notification_failed(
            item, "未配置企业微信 Webhook", max_attempts=settings.worker_max_attempts
        )
        return True
    try:
        response = httpx.post(
            webhook.get_secret_value(),
            json={
                "msgtype": "text",
                "text": {
                    "content": str(item.payload.get("content", "")),
                    "mentioned_list": item.mentioned_userids,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(str(result.get("errmsg", "企业微信发送失败")))
        mark_notification_sent(item, "ok")
    except Exception as exc:
        mark_notification_failed(item, str(exc), max_attempts=settings.worker_max_attempts)
    return True


def main() -> None:
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    factory = make_session_factory(engine)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    with session_scope(factory) as session:
        sync_actor_profiles(session, settings)
    logger.info("worker_started id=%s", worker_id)
    while True:
        with session_scope(factory) as session:
            processed = process_job(session, worker_id)
            if not processed:
                processed = process_notification(session)
        if not processed:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
