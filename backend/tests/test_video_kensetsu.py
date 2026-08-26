import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.services.video_kensetsu import (
    DEFAULT_METADATA_NICKNAME,
    build_encode_form_fields,
    encode,
    find_preset,
    health,
    list_presets,
    video_kensetsu_ui_context,
)


SAMPLE_PRESET = {
    "id": "fast_encode",
    "name": "Быстрое кодирование",
    "description": "fast",
    "video_map": "0:v:0",
    "audio_map": "0:a?",
    "subtitle_map": "0:s?",
    "data_map": "0:t?",
    "video_codec": "libx265",
    "preset": "fast",
    "crf": 28,
    "pixel_format": "yuv420p",
    "x265_params": "level=4.1:ref=2",
    "tune": "none",
    "subtitle_codec": "copy",
    "data_codec": "copy",
    "output_format": "matroska",
    "audio_settings": {
        "codec": "aac",
        "bitrate": "96k",
        "sample_rate": 44100,
        "vbr": True,
    },
}


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)


def test_build_encode_form_fields_flattens_audio_and_skips_none() -> None:
    preset = {
        **SAMPLE_PRESET,
        "audio_map": None,
        "audio_settings": {
            "codec": "aac",
            "bitrate": "96k",
            "sample_rate": None,
            "vbr": True,
        },
    }
    fields = build_encode_form_fields("/anilibria/show/ep.mkv", preset)
    assert fields["path"] == "/anilibria/show/ep.mkv"
    assert fields["preset_id"] == "fast_encode"
    assert fields["preset_modified"] == "false"
    assert fields["cpu_threads"] == "0"
    assert fields["thread_queue_size"] == "0"
    assert fields["x265_pools"] == "0"
    assert fields["update_metadata"] == "true"
    assert fields["metadata_nickname"] == DEFAULT_METADATA_NICKNAME
    assert fields["audio_codec"] == "aac"
    assert fields["audio_bitrate"] == "96k"
    assert fields["audio_vbr"] == "true"
    assert "audio_sample_rate" not in fields
    assert "audio_map" not in fields
    assert "audio_settings" not in fields
    assert "name" not in fields
    assert "description" not in fields
    assert fields["video_codec"] == "libx265"
    assert fields["crf"] == "28"


def test_health_ok(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://encoder.test"
        return httpx.Response(200, request=request, text="ok")

    _patch_httpx(monkeypatch, handler)
    result = asyncio.run(health("http://encoder.test/"))
    assert result["ok"] is True
    assert result["status_code"] == 200


def test_health_non_200(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="down")

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        asyncio.run(health("http://encoder.test"))


def test_list_presets_parses_array(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets"
        return httpx.Response(200, request=request, json=[SAMPLE_PRESET])

    _patch_httpx(monkeypatch, handler)
    presets = asyncio.run(list_presets("http://encoder.test"))
    assert len(presets) == 1
    assert presets[0]["id"] == "fast_encode"
    assert find_preset(presets, "fast_encode") is presets[0]
    assert find_preset(presets, "missing") is None


def test_encode_multipart_fields(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/encode"
        assert request.method == "POST"
        content_type = request.headers.get("content-type", "")
        assert "multipart/form-data" in content_type
        # Parse simple multipart: name="…" then blank line then value
        body = (await request.aread()).decode("utf-8", errors="replace")
        parts = body.split("--")
        for part in parts:
            if 'name="' not in part:
                continue
            name_start = part.index('name="') + 6
            name_end = part.index('"', name_start)
            name = part[name_start:name_end]
            # value after headers block
            if "\r\n\r\n" in part:
                value = part.split("\r\n\r\n", 1)[1].rstrip("\r\n")
            elif "\n\n" in part:
                value = part.split("\n\n", 1)[1].rstrip("\n")
            else:
                continue
            captured[name] = value
        return httpx.Response(200, request=request, json={"job_id": "j1"})

    _patch_httpx(monkeypatch, handler)
    result = asyncio.run(
        encode(
            "http://encoder.test",
            path="/anilibria/2026/show/ep.mkv",
            preset=SAMPLE_PRESET,
        )
    )
    assert result == {"job_id": "j1"}
    assert captured["path"] == "/anilibria/2026/show/ep.mkv"
    assert captured["preset_id"] == "fast_encode"
    assert captured["preset_modified"] == "false"
    assert captured["audio_codec"] == "aac"
    assert captured["audio_bitrate"] == "96k"
    assert captured["audio_sample_rate"] == "44100"
    assert captured["audio_vbr"] == "true"
    assert captured["metadata_nickname"] == DEFAULT_METADATA_NICKNAME
    assert captured["cpu_threads"] == "0"


def test_send_to_encoder_happy_path(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(
        id=7,
        ui_status="ok",
        full_path=str(media),
        info_hash="aa" * 20,
    )

    async def fake_list_presets(_base_url: str):
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, path: str, preset: dict):
        assert base_url == "http://encoder.test"
        assert path == str(media)
        assert preset["id"] == "fast_encode"
        return {"job_id": "ok"}

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
    result = asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert result["ok"] is True
    assert result["preset_id"] == "fast_encode"
    assert result["path"] == str(media)


def test_send_to_encoder_disabled() -> None:
    from app.api import rest as rest_mod
    from unittest import mock

    with mock.patch.object(rest_mod, "is_video_kensetsu_enabled", return_value=False):
        db = MagicMock()
        body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rest_mod.send_torrent_file_to_encoder(1, body, db=db))
        assert exc.value.status_code == 400
        assert "выключен" in str(exc.value.detail).lower()


def test_send_to_encoder_preset_not_found(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(
        id=9,
        ui_status="ok",
        full_path=str(media),
        info_hash="cc" * 20,
    )

    async def fake_list_presets(_base_url: str):
        return [SAMPLE_PRESET]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="missing_preset")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(9, body, db=db))
    assert exc.value.status_code == 404
    assert "Пресет не найден" in str(exc.value.detail)


def test_send_to_encoder_no_path(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    row = SimpleNamespace(
        id=3,
        ui_status="ok",
        full_path=None,
        info_hash="bb" * 20,
    )
    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: None)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(3, body, db=db))
    assert exc.value.status_code == 404


def test_presets_endpoint_disabled(monkeypatch) -> None:
    from app.api import rest as rest_mod

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.video_kensetsu_presets(db=MagicMock()))
    assert exc.value.status_code == 400


def test_presets_endpoint_ok(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list(_base_url: str):
        return [SAMPLE_PRESET]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list)
    payload = asyncio.run(rest_mod.video_kensetsu_presets(db=MagicMock()))
    assert payload["base_url"] == "http://encoder.test"
    assert payload["presets"][0]["id"] == "fast_encode"
    assert payload["presets"][0]["name"] == "Быстрое кодирование"


def test_video_kensetsu_ui_context_uses_health_cache(monkeypatch) -> None:
    from app.services import video_kensetsu as vk_mod

    def fake_get_setting(_db, key: str, default: str = "") -> str:
        values = {
            "video_kensetsu_enabled": "true",
            "video_kensetsu_base_url": "http://encoder.test/",
            "video_kensetsu_health_ok": "true",
            "video_kensetsu_health_checked_at": "2099-01-01T00:00:00+00:00",
        }
        return values.get(key, default)

    monkeypatch.setattr(vk_mod, "get_setting_value", fake_get_setting)
    ctx = video_kensetsu_ui_context(db=MagicMock(), refresh=False)
    assert ctx["video_kensetsu_enabled"] is True
    assert ctx["video_kensetsu_base_url"] == "http://encoder.test"
    assert ctx["video_kensetsu_ok"] is True


def test_video_kensetsu_ui_context_sse_without_cache_is_not_ok(monkeypatch) -> None:
    from app.services import video_kensetsu as vk_mod

    def fake_get_setting(_db, key: str, default: str = "") -> str:
        values = {
            "video_kensetsu_enabled": "true",
            "video_kensetsu_base_url": "http://encoder.test/",
        }
        return values.get(key, default)

    monkeypatch.setattr(vk_mod, "get_setting_value", fake_get_setting)
    ctx = video_kensetsu_ui_context(db=MagicMock(), refresh=False)
    assert ctx["video_kensetsu_ok"] is False


def test_video_kensetsu_ui_context_refresh_probes_when_stale(monkeypatch) -> None:
    from app.services import video_kensetsu as vk_mod

    store: dict[str, str] = {
        "video_kensetsu_enabled": "true",
        "video_kensetsu_base_url": "http://encoder.test",
    }

    def fake_get(_db, key: str, default: str = "") -> str:
        return store.get(key, default)

    def fake_upsert(_db, key: str, value: str) -> None:
        store[key] = value

    monkeypatch.setattr(vk_mod, "get_setting_value", fake_get)
    monkeypatch.setattr(vk_mod, "upsert_setting", fake_upsert)
    monkeypatch.setattr(
        vk_mod,
        "health_sync",
        lambda _url, timeout_sec=2.0: {"ok": True, "status_code": 200, "base_url": "http://encoder.test"},
    )
    db = MagicMock()
    ctx = video_kensetsu_ui_context(db=db, refresh=True)
    assert ctx["video_kensetsu_ok"] is True
    assert store["video_kensetsu_health_ok"] == "true"
    assert store["video_kensetsu_health_checked_at"]


def test_video_kensetsu_ui_context_disabled(monkeypatch) -> None:
    from app.services import video_kensetsu as vk_mod

    monkeypatch.setattr(
        vk_mod,
        "get_setting_value",
        lambda _db, key, default="": "false" if key == "video_kensetsu_enabled" else default,
    )
    ctx = video_kensetsu_ui_context(db=MagicMock())
    assert ctx["video_kensetsu_enabled"] is False
    assert ctx["video_kensetsu_ok"] is False


def test_probe_and_store_health_failure(monkeypatch) -> None:
    from app.services import video_kensetsu as vk_mod

    store: dict[str, str] = {
        "video_kensetsu_enabled": "true",
        "video_kensetsu_base_url": "http://encoder.test",
    }

    def fake_get(_db, key: str, default: str = "") -> str:
        return store.get(key, default)

    def fake_upsert(_db, key: str, value: str) -> None:
        store[key] = value

    monkeypatch.setattr(vk_mod, "get_setting_value", fake_get)
    monkeypatch.setattr(vk_mod, "upsert_setting", fake_upsert)

    def boom(_url, timeout_sec=2.0):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(vk_mod, "health_sync", boom)
    ok = vk_mod.probe_and_store_health(MagicMock(), commit=False)
    assert ok is False
    assert store["video_kensetsu_health_ok"] == "false"