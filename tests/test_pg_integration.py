"""真实 PostgreSQL 集成测试（规格 §3.5/§6.1）。

覆盖 SQLite 无法验证的行为：FOR UPDATE SKIP LOCKED、pg_advisory_xact_lock。
默认跳过；通过以下方式启用：

    make test-pg
    # 或手动：
    TEST_DATABASE_URL=postgresql+psycopg://workflow:pwd@127.0.0.1:5432/workflow_test \
        .venv/bin/python -m pytest tests/test_pg_integration.py
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from workflow.db.models import BackgroundJob, Base, JobStatus
from workflow.jobs import claim_job
from workflow.t2.allocation import advisory_lock

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL 未设置，跳过真实 PostgreSQL 集成测试",
)


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _add_job(engine, job_type: str = "NOOP") -> str:
    with Session(engine) as session:
        job = BackgroundJob(company_id="company-pg", job_type=job_type)
        session.add(job)
        session.commit()
        return job.id


def test_skip_locked_allows_exactly_one_claim_under_concurrency(pg_engine) -> None:
    job_id = _add_job(pg_engine)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        with Session(pg_engine) as session:
            job = claim_job(session, worker_id=name, lease_seconds=30)
            session.commit()
            if job is not None:
                with lock:
                    claimed.append(name)

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 1
    with Session(pg_engine) as session:
        job = session.get(BackgroundJob, job_id)
        assert job.status == JobStatus.RUNNING
        assert job.leased_by == claimed[0]


def test_expired_lease_is_reclaimed_by_another_worker(pg_engine) -> None:
    job_id = _add_job(pg_engine)
    with Session(pg_engine) as session:
        job = session.get(BackgroundJob, job_id)
        job.status = JobStatus.RUNNING
        job.lease_until = datetime.now(UTC) - timedelta(seconds=5)
        job.leased_by = "crashed-worker"
        job.attempts = 1
        session.commit()

    with Session(pg_engine) as session:
        reclaimed = claim_job(session, worker_id="worker-2", lease_seconds=30)
        session.commit()
        assert reclaimed is not None
        assert reclaimed.leased_by == "worker-2"
        assert reclaimed.attempts == 2


def test_advisory_lock_blocks_second_holder_until_commit(pg_engine) -> None:
    key = "allocation:company-pg:2026-07-27"
    acquired_first = threading.Event()
    release_order: list[str] = []

    def holder() -> None:
        with Session(pg_engine) as session:
            advisory_lock(session, key)
            acquired_first.set()
            time.sleep(0.5)
            session.commit()
        release_order.append("holder")

    def waiter() -> None:
        acquired_first.wait(timeout=5)
        with Session(pg_engine) as session:
            advisory_lock(session, key)
            session.commit()
        release_order.append("waiter")

    first = threading.Thread(target=holder)
    second = threading.Thread(target=waiter)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert release_order == ["holder", "waiter"]
