"""Settings round trips and qB probes use isolated in-memory settings only."""

from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import rest
from app.db.models import Setting
from app.services.runtime_settings import SECRET_SETTING_KEYS, mask_settings_dict


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Setting.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_settings_round_trip_preserves_masked_secrets(db):
    stored = {key: f"fixture-{key}" for key in SECRET_SETTING_KEYS}
    stored["anilibria_base_url"] = "https://public.test"
    db.add_all(Setting(key=key, value=value) for key, value in stored.items())
    db.commit()

    masked = rest.get_settings(db)
    assert masked == mask_settings_dict(stored)
    rest.put_settings([rest.SettingsIn(key=key, value=value) for key, value in masked.items()], db)
    assert {key: db.get(Setting, key).value for key in stored} == stored


@pytest.mark.parametrize("value", ["", "***", " *** "])
def test_placeholder_cannot_create_a_secret_override(db, value):
    rest.put_settings([rest.SettingsIn(key="anilibria_bearer_token", value=value)], db)
    assert db.get(Setting, "anilibria_bearer_token") is None


def test_settings_can_replace_secret_and_preserve_blank_secret(db):
    db.add(Setting(key="qb_master_password", value="fixture-old"))
    db.commit()
    rest.put_settings([rest.SettingsIn(key="qb_master_password", value="")], db)
    assert db.get(Setting, "qb_master_password").value == "fixture-old"
    rest.put_settings([rest.SettingsIn(key="qb_master_password", value="fixture-new")], db)
    assert db.get(Setting, "qb_master_password").value == "fixture-new"


@pytest.mark.parametrize("payload, expected_port", [(None, 9091), (rest.QbTestIn(), 9091), (rest.QbTestIn(port=8080), 8080)])
def test_qb_probe_uses_saved_port_unless_explicitly_overridden(monkeypatch, payload, expected_port):
    values = {"qb_master_host": "qb.test", "qb_master_port": "9091", "qb_master_password": "fixture"}
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace(value=values[key]) if key in values else None)
    monkeypatch.setattr(rest, "test_qb_connection", lambda **kwargs: kwargs)
    result = rest.api_qb_test_connection("master", payload, db)
    assert result["port"] == expected_port
    assert result["host"] == "qb.test"


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_qb_probe_reports_invalid_saved_port_as_validation_error(monkeypatch, port):
    values = {"qb_master_host": "qb.test", "qb_master_port": port, "qb_master_password": "fixture"}
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace(value=values[key]) if key in values else None)
    monkeypatch.setattr(rest, "test_qb_connection", lambda **kwargs: pytest.fail("invalid configuration must not connect"))
    with pytest.raises(HTTPException) as exc:
        rest.api_qb_test_connection("master", None, db)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_qb_probe_rejects_out_of_range_input_ports(port):
    with pytest.raises(ValidationError):
        rest.QbTestIn(port=port)
