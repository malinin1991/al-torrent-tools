from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.job_runner import (
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RUNNING,
    JobRunner,
    UnknownJobTypeError,
    cancel_jobs_by_ids,
    cancel_stale_jobs,
    reclaim_orphan_jobs,
    reclaim_stale_jobs,
)


def test_cancel_stale_jobs_marks_running_and_pending() -> None:
    running = SimpleNamespace(id=1, status=STATUS_RUNNING, error=None, finished_at=None)
    pending = SimpleNamespace(id=2, status=STATUS_PENDING, error=None, finished_at=None)
    success = SimpleNamespace(id=3, status="success", error=None, finished_at=None)

    db = MagicMock()
    db.scalars.return_value.all.return_value = [running, pending]

    cancelled = cancel_stale_jobs(db, reason="тест")

    assert cancelled == [1, 2]
    assert running.status == STATUS_CANCELLED
    assert pending.status == STATUS_CANCELLED
    assert running.error == "тест"
    assert pending.finished_at is not None
    assert success.status == "success"
    db.commit.assert_called_once()
    assert db.add.call_count == 2


def test_cancel_stale_jobs_noop_when_empty() -> None:
    db = MagicMock()
    db.scalars.return_value.all.return_value = []

    assert cancel_stale_jobs(db) == []
    db.commit.assert_not_called()


def test_cancel_jobs_by_ids() -> None:
    running = SimpleNamespace(id=7, status=STATUS_RUNNING, error=None, finished_at=None)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [running]

    cancelled = cancel_jobs_by_ids(db, [7], reason="shutdown")

    assert cancelled == [7]
    assert running.status == STATUS_CANCELLED
    assert running.error == "shutdown"
    db.commit.assert_called_once()


def test_reclaim_stale_jobs_by_age() -> None:
    old = datetime.utcnow() - timedelta(minutes=60)
    fresh = datetime.utcnow() - timedelta(minutes=1)
    stale_running = SimpleNamespace(
        id=1, status=STATUS_RUNNING, error=None, finished_at=None, started_at=old, created_at=old
    )
    fresh_pending = SimpleNamespace(
        id=2, status=STATUS_PENDING, error=None, finished_at=None, started_at=None, created_at=fresh
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [stale_running, fresh_pending]
    # Нет логов — для running якорь = started_at (старый).
    db.scalar.return_value = None
    # try_lock успешен → сирота / можно отменить.
    db.execute.return_value.scalar.return_value = True

    cancelled = reclaim_stale_jobs(db, max_age_minutes=30)

    assert cancelled == [1]
    assert stale_running.status == STATUS_CANCELLED
    assert fresh_pending.status == STATUS_PENDING
    db.commit.assert_called_once()


def test_reclaim_keeps_long_running_job_with_recent_logs() -> None:
    """full_sync старше порога, но с свежими логами — не отменяем."""
    old = datetime.utcnow() - timedelta(minutes=120)
    recent_log = datetime.utcnow() - timedelta(minutes=2)
    long_running = SimpleNamespace(
        id=9,
        status=STATUS_RUNNING,
        error=None,
        finished_at=None,
        started_at=old,
        created_at=old,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [long_running]
    db.scalar.return_value = recent_log

    cancelled = reclaim_stale_jobs(db, max_age_minutes=30)

    assert cancelled == []
    assert long_running.status == STATUS_RUNNING
    db.commit.assert_not_called()


def test_reclaim_orphan_running_when_lock_free() -> None:
    running = SimpleNamespace(
        id=3,
        status=STATUS_RUNNING,
        error=None,
        finished_at=None,
        started_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [running]
    db.execute.return_value.scalar.return_value = True

    cancelled = reclaim_orphan_jobs(db, reason="сирота")

    assert cancelled == [3]
    assert running.status == STATUS_CANCELLED
    db.commit.assert_called_once()


def test_reclaim_orphan_skips_running_when_lock_held() -> None:
    running = SimpleNamespace(
        id=4,
        status=STATUS_RUNNING,
        error=None,
        finished_at=None,
        started_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [running]
    db.execute.return_value.scalar.return_value = False

    cancelled = reclaim_orphan_jobs(db)

    assert cancelled == []
    assert running.status == STATUS_RUNNING
    db.commit.assert_not_called()


def test_reclaim_orphan_pending_older_than_grace() -> None:
    old = datetime.utcnow() - timedelta(seconds=120)
    pending = SimpleNamespace(
        id=5, status=STATUS_PENDING, error=None, finished_at=None, started_at=None, created_at=old
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [pending]

    cancelled = reclaim_orphan_jobs(db, pending_grace_sec=60)

    assert cancelled == [5]
    assert pending.status == STATUS_CANCELLED


def test_create_job_rejects_unknown_type() -> None:
    runner = JobRunner()
    db = MagicMock()
    with pytest.raises(UnknownJobTypeError):
        runner.create_job(db, "not_a_real_job", {})
