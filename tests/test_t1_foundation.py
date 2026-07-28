from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from workflow.config import Settings
from workflow.db.models import (
    ActorProfile,
    BackgroundJob,
    Base,
    ContentProject,
    JobStatus,
    Notification,
    NotificationStatus,
)
from workflow.errors import IdempotencyConflict
from workflow.idempotency import replay_or_none, save_response
from workflow.identity import sync_actor_profiles, token_digest
from workflow.jobs import claim_job, fail_job, succeed_job
from workflow.notifications import (
    claim_notification,
    mark_notification_failed,
    mark_notification_sent,
)
from workflow.storage import LocalStorage


@pytest.fixture
def engine():
    value = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(value)
    return value


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        company_id="company-test",
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-token-at-least-16",'
            '"wecom_userid":"employee-a","active":true}]'
        ),
    )


def test_actor_sync_does_not_store_plaintext_tokens(engine) -> None:
    with Session(engine) as session:
        profiles = sync_actor_profiles(session, settings())
        session.commit()

        assert len(profiles) == 2
        stored = session.scalars(select(ActorProfile)).all()
        assert {item.display_name for item in stored} == {"老板测试", "员工甲"}
        assert {item.token_sha256 for item in stored} == {
            token_digest("boss-token-at-least-16"),
            token_digest("employee-token-at-least-16"),
        }


def test_idempotency_replays_and_rejects_fingerprint_conflict(engine) -> None:
    with Session(engine) as session:
        actor = sync_actor_profiles(session, settings())[0]
        session.flush()
        save_response(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool="probe",
            key="same-key",
            payload={"value": "A"},
            response={"success": True},
        )
        session.commit()

        assert replay_or_none(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool="probe",
            key="same-key",
            payload={"value": "A"},
        ) == {"success": True}
        with pytest.raises(IdempotencyConflict):
            replay_or_none(
                session,
                company_id=actor.company_id,
                actor_id=actor.id,
                tool="probe",
                key="same-key",
                payload={"value": "B"},
            )


def test_idempotency_duplicate_save_replays_first_response(engine) -> None:
    with Session(engine) as session:
        actor = sync_actor_profiles(session, settings())[0]
        session.flush()
        first = save_response(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool="probe",
            key="race-key",
            payload={"value": "A"},
            response={"success": True, "nonce": "first"},
        )
        assert first is None
        session.commit()

        # 模拟并发下唯一约束冲突：第二个保存必须回退为重放首次响应。
        second = save_response(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool="probe",
            key="race-key",
            payload={"value": "A"},
            response={"success": True, "nonce": "second"},
        )
        assert second == {"success": True, "nonce": "first"}
        with pytest.raises(IdempotencyConflict):
            save_response(
                session,
                company_id=actor.company_id,
                actor_id=actor.id,
                tool="probe",
                key="race-key",
                payload={"value": "B"},
                response={"success": True},
            )


def test_worker_reclaims_expired_lease_and_retries(engine) -> None:
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with Session(engine) as session:
        job = BackgroundJob(
            company_id="company-test",
            job_type="NOOP",
            status=JobStatus.RUNNING,
            lease_until=expired,
            attempts=1,
            max_attempts=3,
        )
        session.add(job)
        session.commit()

        claimed = claim_job(session, worker_id="worker-2", lease_seconds=30)
        assert claimed is not None
        assert claimed.leased_by == "worker-2"
        assert claimed.attempts == 2
        fail_job(claimed, "temporary")
        assert claimed.status == JobStatus.FAILED
        claimed.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

        claimed = claim_job(session, worker_id="worker-3", lease_seconds=30)
        assert claimed is not None
        succeed_job(claimed)
        assert claimed.status == JobStatus.SUCCEEDED


def test_sqlalchemy_optimistic_lock_rejects_stale_update(engine) -> None:
    with Session(engine) as setup:
        project = ContentProject(company_id="company-test", title="项目", status="TOPIC_IMPORTED")
        setup.add(project)
        setup.commit()
        project_id = project.id

    with Session(engine) as first, Session(engine) as second:
        item_a = first.get(ContentProject, project_id)
        item_b = second.get(ContentProject, project_id)
        assert item_a is not None and item_b is not None
        item_a.status = "SCRIPT_IN_PROGRESS"
        first.commit()
        item_b.status = "CANCELLED"
        with pytest.raises(StaleDataError):
            second.commit()


def test_notification_outbox_claim_retry_and_success(engine) -> None:
    with Session(engine) as session:
        item = Notification(
            company_id="company-test",
            event_id="event-1",
            template="TASK_CREATED",
            payload={"content": "任务已创建"},
            mentioned_userids=["employee-a"],
            status=NotificationStatus.PENDING,
        )
        session.add(item)
        session.commit()

        claimed = claim_notification(session)
        assert claimed is not None
        mark_notification_failed(claimed, "temporary", max_attempts=3)
        assert claimed.status == NotificationStatus.FAILED
        claimed.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

        claimed = claim_notification(session)
        assert claimed is not None
        mark_notification_sent(claimed, "ok")
        assert claimed.status == NotificationStatus.SENT


def test_local_storage_uses_opaque_id_and_relative_storage_key(tmp_path) -> None:
    storage = LocalStorage(tmp_path, reject_percent=1)
    result = storage.put(
        io.BytesIO(b"t1-persistent-file"),
        company_id="company-test",
        purpose="script",
        attachment_id="attachment-1",
    )

    assert result.storage_provider == "LOCAL"
    assert not result.storage_key.startswith("/")
    assert len(result.opaque_file_id) >= 32
    assert storage.path_for(result.storage_key).read_bytes() == b"t1-persistent-file"
    with pytest.raises(ValueError):
        storage.path_for("../../etc/passwd")
