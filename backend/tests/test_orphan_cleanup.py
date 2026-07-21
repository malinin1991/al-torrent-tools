from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from app.jobs.orphan_cleanup import run_orphan_cleanup


def test_orphan_cleanup_forces_dry_run_when_delete_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "orphan.mkv"
    target.write_bytes(b"orphan")

    db = MagicMock()
    logs: list[str] = []

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr("app.jobs.orphan_cleanup.collect_known_paths", lambda _db: set())
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", False)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert target.exists()
    assert any("CLEANUP_ALLOW_DELETE=false" in msg for msg in logs)
