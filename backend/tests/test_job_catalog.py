"""Тесты каталога джобов для UI."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.job_catalog import JOB_TYPE_DEFS, compute_next_run_at, load_job_catalog
from app.services.job_runner import STATUS_RUNNING, STATUS_SUCCESS


def test_job_catalog_covers_known_manual_types() -> None:
    types = {item.type for item in JOB_TYPE_DEFS}
    assert "ongoing" in types
    assert "orphan_cleanup" in types
    assert "cleanup_master" in types
    assert "cleanup_slave" in types
    assert "cleanup_logs" in types
    assert "cleanup" not in types
    assert "hash_torrent" in types
    hash_def = next(item for item in JOB_TYPE_DEFS if item.type == "hash_torrent")
    assert hash_def.manual_run is False
    orphan = next(item for item in JOB_TYPE_DEFS if item.type == "orphan_cleanup")
    assert "dry_run" in orphan.run_modes
    assert "apply" in orphan.run_modes
    master = next(item for item in JOB_TYPE_DEFS if item.type == "cleanup_master")
    assert master.run_modes == ("dry_run", "apply")


def test_compute_next_run_at_from_finished() -> None:
    finished = datetime(2026, 7, 22, 12, 0, 0)
    job = SimpleNamespace(status=STATUS_SUCCESS, started_at=None, finished_at=finished)
    assert compute_next_run_at(job, interval_sec=3600) == finished + timedelta(seconds=3600)


def test_compute_next_run_at_running_uses_started() -> None:
    started = datetime(2026, 7, 22, 12, 0, 0)
    job = SimpleNamespace(status=STATUS_RUNNING, started_at=started, finished_at=None)
    assert compute_next_run_at(job, interval_sec=120) == started + timedelta(seconds=120)


def test_load_job_catalog_attaches_last_job() -> None:
    db = MagicMock()
    last_job = SimpleNamespace(
        id=42,
        type="ongoing",
        status=STATUS_RUNNING,
        started_at=None,
        finished_at=None,
        error=None,
    )

    call_count = {"n": 0}

    def fake_scalar(_stmt):
        call_count["n"] += 1
        # Первый вызов — last_job для ongoing; остальные — None / settings.
        if call_count["n"] == 1:
            return last_job
        return None

    db.scalar.side_effect = fake_scalar

    entries = load_job_catalog(db)
    assert len(entries) == len(JOB_TYPE_DEFS)
    first = entries[0]
    assert first["def"].type == "ongoing"
    assert first["last_job"] is last_job
    assert first["is_active"] is True
    assert first["can_stop"] is True
    assert entries[1]["last_job"] is None
    assert entries[1]["can_stop"] is False
    assert "logs" not in first
