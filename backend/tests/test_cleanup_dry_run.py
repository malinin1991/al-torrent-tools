import asyncio
from unittest.mock import MagicMock

from app.jobs.cleanup import run_cleanup


class _FakeSessionCM:
    def __enter__(self) -> MagicMock:
        return MagicMock()

    def __exit__(self, *args: object) -> bool:
        return False


def test_run_cleanup_defaults_dry_run_true(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            self.db = db
            self.job_id = job_id

        def run(self, dry_run: bool = True, *, target_role: str | None = None) -> dict[str, int]:
            captured["dry_run"] = dry_run
            captured["target_role"] = target_role
            return {"checked": 0, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.SessionLocal", _FakeSessionCM)

    asyncio.run(run_cleanup(MagicMock(), job_id=1, params={"target_role": "master"}))

    assert captured["dry_run"] is True
    assert captured["target_role"] == "master"


def test_run_cleanup_forces_dry_run_when_delete_disabled(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            pass

        def run(self, dry_run: bool = True, *, target_role: str | None = None) -> dict[str, int]:
            captured["dry_run"] = dry_run
            captured["target_role"] = target_role
            return {"checked": 1, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.SessionLocal", _FakeSessionCM)
    monkeypatch.setattr("app.jobs.cleanup.settings.cleanup_allow_delete", False)

    asyncio.run(
        run_cleanup(MagicMock(), job_id=2, params={"dry_run": False, "target_role": "slave"})
    )

    assert captured["dry_run"] is True
    assert captured["target_role"] == "slave"


def test_run_cleanup_allows_delete_when_enabled(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            pass

        def run(self, dry_run: bool = True, *, target_role: str | None = None) -> dict[str, int]:
            captured["dry_run"] = dry_run
            captured["target_role"] = target_role
            return {"checked": 1, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.SessionLocal", _FakeSessionCM)
    monkeypatch.setattr("app.jobs.cleanup.settings.cleanup_allow_delete", True)

    asyncio.run(
        run_cleanup(MagicMock(), job_id=3, params={"dry_run": False, "target_role": "master"})
    )

    assert captured["dry_run"] is False
    assert captured["target_role"] == "master"


def test_run_cleanup_infers_role_from_job_type(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, db: object, job_id: int) -> None:
            pass

        def run(self, dry_run: bool = True, *, target_role: str | None = None) -> dict[str, int]:
            captured["target_role"] = target_role
            return {"checked": 0, "matched": 0, "deleted": 0}

        def add_log(self, message: str) -> None:
            return None

    job = MagicMock()
    job.type = "cleanup_slave"
    db = MagicMock()
    db.get.return_value = job

    monkeypatch.setattr("app.jobs.cleanup.TorrentCleanupService", FakeService)
    monkeypatch.setattr("app.jobs.cleanup.SessionLocal", _FakeSessionCM)

    asyncio.run(run_cleanup(db, job_id=4, params={}))

    assert captured["target_role"] == "slave"
