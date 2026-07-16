import asyncio
from unittest.mock import MagicMock

from app.jobs.cleanup import run_cleanup


def test_run_cleanup_defaults_dry_run_true(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            self.db = db
            self.job_id = job_id

        def run(self, dry_run: bool = True) -> dict[str, int]:
            captured["dry_run"] = dry_run
            return {"checked": 0, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)

    asyncio.run(run_cleanup(MagicMock(), job_id=1, params={}))

    assert captured["dry_run"] is True


def test_run_cleanup_forces_dry_run_when_delete_disabled(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            pass

        def run(self, dry_run: bool = True) -> dict[str, int]:
            captured["dry_run"] = dry_run
            return {"checked": 1, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.settings.cleanup_allow_delete", False)

    asyncio.run(run_cleanup(MagicMock(), job_id=2, params={"dry_run": False}))

    assert captured["dry_run"] is True


def test_run_cleanup_allows_delete_when_enabled(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            pass

        def run(self, dry_run: bool = True) -> dict[str, int]:
            captured["dry_run"] = dry_run
            return {"checked": 1, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.settings.cleanup_allow_delete", True)

    asyncio.run(run_cleanup(MagicMock(), job_id=3, params={"dry_run": False}))

    assert captured["dry_run"] is False
