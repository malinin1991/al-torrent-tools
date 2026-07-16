from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.job_runner import (
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RUNNING,
    cancel_stale_jobs,
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
