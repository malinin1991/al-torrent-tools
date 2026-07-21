from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from app.services.system_status import _APP_PACKAGES, _pkg_version, _telegram_bot_process_status


def test_pkg_versions_resolve() -> None:
    assert "fastapi" in _APP_PACKAGES
    assert "python-telegram-bot" in _APP_PACKAGES
    assert _pkg_version("fastapi") not in {"", None}
    assert _pkg_version("definitely-missing-package-xyz") == "—"


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
