from types import SimpleNamespace

from app.services.runtime_settings import (
    mask_settings_dict,
    resolve_anilibria_settings,
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
            "scrape_pause_every": "10",
        }
    )
    assert masked["anilibria_base_url"] == "https://example.test"
    assert masked["anilibria_bearer_token"] == "***"
    assert masked["anilibria_password"] == "***"
    assert masked["qb_master_password"] == "***"
    assert masked["qb_slave_password"] == ""
    assert masked["telegram_bot_token"] == "***"
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
