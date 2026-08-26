from types import SimpleNamespace

from app.services.runtime_settings import (
    mask_settings_dict,
    resolve_anilibria_settings,
    resolve_telegram_bot_settings,
)


def test_mask_settings_dict_hides_secrets() -> None:
    masked = mask_settings_dict(
        {
            "anilibria_base_url": "https://example.test",
            "anilibria_bearer_token": "secret-token",
            "anilibria_password": "al-pass",
            "qb_master_password": "master-pass",
            "qb_slave_password": "",
            "telegram_bot_token": "tg-secret",
            "telegram_hevc_bot_token": "hevc-secret",
            "scrape_pause_every": "10",
        }
    )
    assert masked["anilibria_base_url"] == "https://example.test"
    assert masked["anilibria_bearer_token"] == "***"
    assert masked["anilibria_password"] == "***"
    assert masked["qb_master_password"] == "***"
    assert masked["qb_slave_password"] == ""
    assert masked["telegram_bot_token"] == "***"
    assert masked["telegram_hevc_bot_token"] == "***"
    assert masked["scrape_pause_every"] == "10"


def test_resolve_anilibria_settings_prefers_db_over_env(monkeypatch) -> None:
    from app.core import config as config_module

    monkeypatch.setattr(config_module.settings, "anilibria_base_url", "https://env.example/api")
    monkeypatch.setattr(config_module.settings, "anilibria_fallback_base_url", "https://env-fallback.example/api")
    monkeypatch.setattr(config_module.settings, "anilibria_bearer_token", "env-token")

    class FakeDb:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def get(self, model, key):  # noqa: ANN001
            _ = model
            value = self._values.get(key)
            if value is None:
                return None
            return SimpleNamespace(value=value)

    resolved = resolve_anilibria_settings(
        FakeDb(
            {
                "anilibria_base_url": "https://db.example/api",
                "anilibria_bearer_token": "db-token",
            }
        )
    )
    assert resolved.base_url == "https://db.example/api"
    assert resolved.fallback_base_url == "https://env-fallback.example/api"
    assert resolved.bearer_token == "db-token"


def test_get_setting_value_allow_empty_keeps_blank_over_default() -> None:
    from app.services.runtime_settings import get_setting_value

    class FakeDb:
        def get(self, model, key):  # noqa: ANN001
            _ = model, key
            return SimpleNamespace(value="")

    assert get_setting_value(FakeDb(), "anilibria_admin_url_template", "env-tpl") == "env-tpl"
    assert (
        get_setting_value(FakeDb(), "anilibria_admin_url_template", "env-tpl", allow_empty=True)
        == ""
    )


def test_telegram_profiles_use_separate_settings_and_heartbeat() -> None:
    class FakeDb:
        values = {
            "telegram_enabled": "true",
            "telegram_bot_token": "primary-token",
            "telegram_bot_api_base_url": "https://primary.example",
            "telegram_hevc_enabled": "on",
            "telegram_hevc_bot_token": "hevc-token",
            "telegram_hevc_bot_api_base_url": "https://hevc.example",
        }

        def get(self, model, key):  # noqa: ANN001
            _ = model
            value = self.values.get(key)
            return SimpleNamespace(value=value) if value is not None else None

    db = FakeDb()
    primary = resolve_telegram_bot_settings(db, "primary")
    hevc = resolve_telegram_bot_settings(db, "hevc")
    assert (primary.token, primary.heartbeat_key) == (
        "primary-token",
        "telegram_bot_heartbeat_at",
    )
    assert (hevc.token, hevc.heartbeat_key) == (
        "hevc-token",
        "telegram_hevc_bot_heartbeat_at",
    )
    assert primary.api_base_url == "https://primary.example"
    assert hevc.api_base_url == "https://hevc.example"
