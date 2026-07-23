from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from app.services.system_status import (
    _APP_PACKAGES,
    _pkg_version,
    _telegram_bot_process_status,
    resolve_build_info,
)


def test_pkg_versions_resolve() -> None:
    assert "fastapi" in _APP_PACKAGES
    assert "python-telegram-bot" in _APP_PACKAGES
    assert _pkg_version("fastapi") not in {"", None}
    assert _pkg_version("definitely-missing-package-xyz") == "—"


def test_resolve_build_info_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_BUILD_TIME", "2026-07-23T12:00:00Z")
    monkeypatch.setenv("APP_GIT_SHA", "abc1234")
    monkeypatch.setattr("app.services.system_status._BUILD_TIME_FILE", tmp_path / "missing")
    monkeypatch.setattr("app.services.system_status._GIT_SHA_FILE", tmp_path / "missing")
    info = resolve_build_info()
    assert info["time"] == "2026-07-23T12:00:00Z"
    assert info["git_sha"] == "abc1234"


def test_resolve_build_info_from_file(monkeypatch, tmp_path: Path) -> None:
    stamp = tmp_path / "build_time"
    sha = tmp_path / "git_sha"
    stamp.write_text("2026-07-22T08:15:30Z", encoding="utf-8")
    sha.write_text("deadbeef", encoding="utf-8")
    monkeypatch.delenv("APP_BUILD_TIME", raising=False)
    monkeypatch.delenv("APP_GIT_SHA", raising=False)
    monkeypatch.setattr("app.services.system_status._BUILD_TIME_FILE", stamp)
    monkeypatch.setattr("app.services.system_status._GIT_SHA_FILE", sha)
    info = resolve_build_info()
    assert info["time"] == "2026-07-22T08:15:30Z"
    assert info["git_sha"] == "deadbeef"


def test_telegram_bot_process_status_fresh(monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    monkeypatch.setattr(
        "app.services.system_status.get_setting_value",
        lambda db, key, default="": stamp if key == "telegram_bot_heartbeat_at" else default,
    )
    status = _telegram_bot_process_status(MagicMock())
    assert status["ok"] is True


def test_telegram_bot_process_status_stale(monkeypatch) -> None:
    old = datetime.now(timezone.utc) - timedelta(seconds=500)
    stamp = old.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    monkeypatch.setattr(
        "app.services.system_status.get_setting_value",
        lambda db, key, default="": stamp if key == "telegram_bot_heartbeat_at" else default,
    )
    status = _telegram_bot_process_status(MagicMock())
    assert status["ok"] is False
    assert "устарел" in status["detail"]
