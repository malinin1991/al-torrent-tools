"""Тесты каталога джобов для UI."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.job_catalog import JOB_TYPE_DEFS, load_job_catalog
from app.services.job_runner import STATUS_RUNNING


def test_job_catalog_covers_known_manual_types() -> None:
    types = {item.type for item in JOB_TYPE_DEFS}
    assert "ongoing" in types
    assert "orphan_cleanup" in types
    assert "hash_torrent" in types
    hash_def = next(item for item in JOB_TYPE_DEFS if item.type == "hash_torrent")
    assert hash_def.manual_run is False
    orphan = next(item for item in JOB_TYPE_DEFS if item.type == "orphan_cleanup")
    assert "dry_run" in orphan.run_modes
    assert "apply" in orphan.run_modes


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
    assert entries[1]["last_job"] is None
    assert "logs" not in first
