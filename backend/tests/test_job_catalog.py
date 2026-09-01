"""Тесты каталога джобов для UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import job_catalog
from app.services.job_catalog import (
    FULL_SYNC_DAILY_HOUR,
    FULL_SYNC_DAILY_MINUTE,
    JOB_TYPE_DEFS,
    _local_naive_to_utc_naive,
    compute_next_daily_run_at,
    compute_next_run_at,
    load_job_catalog,
)
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
    assert "meta_sync" in types
    assert "full_meta_sync" in types
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


def test_compute_next_daily_run_at_same_day(monkeypatch) -> None:
    monkeypatch.setattr(job_catalog, "_local_naive_to_utc_naive", lambda dt: dt)
    now = datetime(2026, 7, 22, 7, 30, 0)
    assert compute_next_daily_run_at(hour=8, minute=0, now=now) == datetime(2026, 7, 22, 8, 0, 0)


def test_compute_next_daily_run_at_next_day(monkeypatch) -> None:
    monkeypatch.setattr(job_catalog, "_local_naive_to_utc_naive", lambda dt: dt)
    now = datetime(2026, 7, 22, 8, 1, 0)
    assert compute_next_daily_run_at(hour=8, minute=0, now=now) == datetime(2026, 7, 23, 8, 0, 0)


def test_local_naive_to_utc_naive_fixed_offset(monkeypatch) -> None:
    fixed_tz = timezone(timedelta(hours=7))
    now_mock = MagicMock()
    now_mock.astimezone.return_value.tzinfo = fixed_tz

    class PatchedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_mock

    monkeypatch.setattr(job_catalog, "datetime", PatchedDatetime)
    local_dt = datetime(2026, 7, 22, 8, 0, 0)
    assert _local_naive_to_utc_naive(local_dt) == datetime(2026, 7, 22, 1, 0, 0)


def test_compute_next_daily_run_at_converts_to_utc(monkeypatch) -> None:
    fixed_tz = timezone(timedelta(hours=7))
    now_mock = MagicMock()
    now_mock.astimezone.return_value.tzinfo = fixed_tz

    class PatchedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_mock

    monkeypatch.setattr(job_catalog, "datetime", PatchedDatetime)
    now = datetime(2026, 7, 22, 7, 30, 0)
    assert compute_next_daily_run_at(hour=8, minute=0, now=now) == datetime(2026, 7, 22, 1, 0, 0)


def test_full_sync_description_mentions_daily_schedule() -> None:
    full_sync = next(item for item in JOB_TYPE_DEFS if item.type == "full_sync")
    assert "08:00" in full_sync.description
    assert "force_qb_load" in full_sync.description


def test_load_job_catalog_full_sync_next_run_at(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.job_catalog.compute_next_daily_run_at",
        lambda **kwargs: datetime(2026, 7, 23, FULL_SYNC_DAILY_HOUR, FULL_SYNC_DAILY_MINUTE),
    )
    db = MagicMock()
    db.scalar.return_value = None
    entries = load_job_catalog(db)
    full_sync_entry = next(item for item in entries if item["def"].type == "full_sync")
    assert full_sync_entry["next_run_at"] == datetime(
        2026, 7, 23, FULL_SYNC_DAILY_HOUR, FULL_SYNC_DAILY_MINUTE
    )


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
