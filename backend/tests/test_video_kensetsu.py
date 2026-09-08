import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.services.video_kensetsu import (
    ENCODE_BATCH_MAX_FILES,
    VideoKensetsuHttpError,
    _parse_defaults,
    audio_downmix_forbidden_for_audio,
    coerce_preset_available,
    encode,
    encode_timeout_for_paths,
    find_layer_preset,
    health,
    health_sync,
    list_presets,
    normalize_audio_downmix,
    video_kensetsu_ui_context,
)


SAMPLE_VIDEO = {
    "id": "hevc_rip",
    "name": "HEVC rip",
    "default": True,
    "video_codec": "libx265",
    "preset": "slow",
    "crf": "23",
}

SAMPLE_VIDEO_REMUX = {
    "id": "hevc_remux",
    "name": "HEVC remux",
    "default": False,
    "video_codec": "libx265",
    "preset": "slower",
    "crf": "21",
}

SAMPLE_AUDIO_COPY = {
    "id": "copy",
    "name": "Copy",
    "default": True,
    "codec": "copy",
    "available": True,
}

SAMPLE_AUDIO_OPUS = {
    "id": "opus",
    "name": "Opus",
    "default": False,
    "codec": "libopus",
    "available": True,
}

SAMPLE_AUDIO_FDK = {
    "id": "aac_fdk",
    "name": "AAC FDK",
    "default": False,
    "codec": "libfdk_aac",
    "available": False,
}

SAMPLE_LAYERS = {
    "videos": [SAMPLE_VIDEO, SAMPLE_VIDEO_REMUX],
    "audios": [SAMPLE_AUDIO_COPY, SAMPLE_AUDIO_OPUS, SAMPLE_AUDIO_FDK],
    "defaults": {
        "video_id": "hevc_rip",
        "audio_id": "copy",
        "audio_downmix": "none",
    },
}


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)


def test_health_ok(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://encoder.test/health"
        return httpx.Response(200, request=request, text="ok")

    _patch_httpx(monkeypatch, handler)
    result = asyncio.run(health("http://encoder.test/"))
    assert result["ok"] is True
    assert result["status_code"] == 200


def test_health_sync_hits_health(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://encoder.test/health"
        return httpx.Response(200, request=request, text="ok")

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def build_client(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", build_client)
    result = health_sync("http://encoder.test/")
    assert result["ok"] is True
    assert result["base_url"] == "http://encoder.test"


def test_health_non_200(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="down")

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        asyncio.run(health("http://encoder.test"))


def test_list_presets_parses_layers(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets"
        return httpx.Response(200, request=request, json=SAMPLE_LAYERS)

    _patch_httpx(monkeypatch, handler)
    layers = asyncio.run(list_presets("http://encoder.test"))
    assert len(layers["videos"]) == 2
    assert layers["videos"][0]["id"] == "hevc_rip"
    assert layers["audios"][0]["id"] == "copy"
    assert layers["defaults"]["video_id"] == "hevc_rip"
    assert layers["defaults"]["audio_id"] == "copy"
    assert layers["defaults"]["audio_downmix"] == "none"
    assert find_layer_preset(layers["videos"], "hevc_rip") is layers["videos"][0]
    assert find_layer_preset(layers["audios"], "missing") is None


def test_list_presets_rejects_flat_list(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=[SAMPLE_VIDEO])

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Некорректный ответ"):
        asyncio.run(list_presets("http://encoder.test"))


def test_list_presets_rejects_empty_videos(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"videos": [], "audios": [SAMPLE_AUDIO_COPY], "defaults": {}},
        )

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="videos и audios"):
        asyncio.run(list_presets("http://encoder.test"))


def test_list_presets_rejects_empty_audios(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"videos": [SAMPLE_VIDEO], "audios": [], "defaults": {}},
        )

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="videos и audios"):
        asyncio.run(list_presets("http://encoder.test"))


def test_parse_defaults_invalid_audio_downmix_becomes_none() -> None:
    parsed = _parse_defaults(
        {"video_id": "hevc_rip", "audio_id": "copy", "audio_downmix": "mono"}
    )
    assert parsed["audio_downmix"] == "none"
    assert parsed["video_id"] == "hevc_rip"
    assert parsed["audio_id"] == "copy"


def test_encode_json_single_path(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/encode"
        assert request.method == "POST"
        assert "application/json" in request.headers.get("content-type", "")
        captured["json"] = json.loads((await request.aread()).decode("utf-8"))
        return httpx.Response(200, request=request, json={"job_id": "j1", "created": True})

    _patch_httpx(monkeypatch, handler)
    result = asyncio.run(
        encode(
            "http://encoder.test",
            paths=["/anilibria/2026/show/ep.mkv"],
            video_id="hevc_rip",
            audio_id="opus",
            audio_downmix="stereo",
        )
    )
    assert result["status_code"] == 200
    assert result["body"] == {"job_id": "j1", "created": True}
    assert captured["json"] == {
        "path": "/anilibria/2026/show/ep.mkv",
        "video_id": "hevc_rip",
        "audio_id": "opus",
        "audio_downmix": "stereo",
    }


def test_encode_json_batch_paths_and_207(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads((await request.aread()).decode("utf-8"))
        return httpx.Response(
            207,
            request=request,
            json={
                "created": 1,
                "jobs": [{"id": "ok"}],
                "errors": [{"path": "/bad.mkv", "error": "missing"}],
            },
        )

    _patch_httpx(monkeypatch, handler)
    result = asyncio.run(
        encode(
            "http://encoder.test",
            paths=["/anilibria/a.mkv", "/anilibria/b.mkv"],
            video_id="hevc_remux",
            audio_id="copy",
        )
    )
    assert result["status_code"] == 207
    assert result["body"]["created"] == 1
    assert len(result["body"]["errors"]) == 1
    assert captured["json"] == {
        "paths": ["/anilibria/a.mkv", "/anilibria/b.mkv"],
        "video_id": "hevc_remux",
        "audio_id": "copy",
        "audio_downmix": "none",
    }


def test_encode_rejects_stereo_with_copy() -> None:
    with pytest.raises(ValueError, match="stereo"):
        asyncio.run(
            encode(
                "http://encoder.test",
                paths=["/a.mkv"],
                video_id="hevc_rip",
                audio_id="copy",
                audio_downmix="stereo",
            )
        )


def test_encode_http_error_maps_body(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={"error": "Пресет FDK недоступен"},
        )

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(VideoKensetsuHttpError) as exc:
        asyncio.run(
            encode(
                "http://encoder.test",
                paths=["/anilibria/a.mkv"],
                video_id="hevc_rip",
                audio_id="aac_fdk",
            )
        )
    assert exc.value.status_code == 400
    assert "FDK" in exc.value.message


def test_encode_http_403_maps_body(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={"error": "Internal encode выключен"})

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(VideoKensetsuHttpError) as exc:
        asyncio.run(
            encode(
                "http://encoder.test",
                paths=["/a.mkv"],
                video_id="hevc_rip",
                audio_id="copy",
            )
        )
    assert exc.value.status_code == 403
    assert "выключен" in exc.value.message.lower()


def test_encode_timeout_scales_with_batch() -> None:
    assert encode_timeout_for_paths(1) == 60.0
    assert encode_timeout_for_paths(3) == 90.0
    assert encode_timeout_for_paths(100) == 300.0


def test_coerce_preset_available() -> None:
    assert coerce_preset_available(True) is True
    assert coerce_preset_available(False) is False
    assert coerce_preset_available(None) is True
    assert coerce_preset_available(0) is False
    assert coerce_preset_available(1) is True
    assert coerce_preset_available("false") is False
    assert coerce_preset_available("0") is False
    assert coerce_preset_available("true") is True
    assert coerce_preset_available("") is False


def test_normalize_audio_downmix_and_stereo_rule() -> None:
    assert normalize_audio_downmix(None) == "none"
    assert normalize_audio_downmix("STEREO") == "stereo"
    with pytest.raises(ValueError):
        normalize_audio_downmix("mono")
    assert audio_downmix_forbidden_for_audio("copy", "stereo") is True
    assert audio_downmix_forbidden_for_audio("none", "stereo") is True
    assert audio_downmix_forbidden_for_audio("opus", "stereo") is False
    assert audio_downmix_forbidden_for_audio("copy", "none") is False


def _fake_layers(**overrides):
    layers = {
        "videos": [SAMPLE_VIDEO, SAMPLE_VIDEO_REMUX],
        "audios": [SAMPLE_AUDIO_COPY, SAMPLE_AUDIO_OPUS, SAMPLE_AUDIO_FDK],
        "defaults": dict(SAMPLE_LAYERS["defaults"]),
    }
    layers.update(overrides)
    return layers


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
        return _fake_layers()

    async def fake_encode(
        base_url: str,
        *,
        paths: list[str],
        video_id: str,
        audio_id: str,
        audio_downmix: str = "none",
        **_kwargs,
    ):
        assert base_url == "http://encoder.test"
        assert paths == [str(media)]
        assert video_id == "hevc_rip"
        assert audio_id == "opus"
        assert audio_downmix == "stereo"
        return {"status_code": 200, "body": {"job_id": "ok", "created": True}}

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(
        video_id="hevc_rip",
        audio_id="opus",
        audio_downmix="stereo",
    )
    response = asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert response.status_code == 200
    result = json.loads(response.body)
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["video_id"] == "hevc_rip"
    assert result["audio_id"] == "opus"
    assert result["audio_downmix"] == "stereo"
    assert "path" not in result
    assert result["created"] == 1
    assert result["result"] == {"job_id": "ok", "created": True}


def test_send_to_encoder_partial_207(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=7, full_path=str(media), info_hash="aa" * 20)

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(base_url: str, *, paths: list[str], video_id: str, audio_id: str, **_kwargs):
        return {
            "status_code": 207,
            "body": {
                "created": 0,
                "jobs": [],
                "errors": [{"path": str(media.resolve()), "error": "bad type"}],
            },
        }

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="copy")
    response = asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert response.status_code == 207
    data = json.loads(response.body)
    assert data["ok"] is False
    assert data["partial"] is True
    assert data["errors"]


def test_send_to_encoder_encoder_400(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod
    from app.services.video_kensetsu import VideoKensetsuHttpError

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=7, full_path=str(media), info_hash="aa" * 20)

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(base_url: str, *, paths: list[str], video_id: str, audio_id: str, **_kwargs):
        raise VideoKensetsuHttpError("Пресет FDK недоступен", status_code=400)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="opus")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert exc.value.status_code == 400
    assert "FDK" in str(exc.value.detail)


def test_send_to_encoder_encoder_403(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod
    from app.services.video_kensetsu import VideoKensetsuHttpError

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=7, full_path=str(media), info_hash="aa" * 20)

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(base_url: str, *, paths: list[str], video_id: str, audio_id: str, **_kwargs):
        raise VideoKensetsuHttpError("путь вне корней", status_code=403)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="copy")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert exc.value.status_code == 403


def test_send_to_encoder_disabled() -> None:
    from app.api import rest as rest_mod
    from unittest import mock

    with mock.patch.object(rest_mod, "is_video_kensetsu_enabled", return_value=False):
        db = MagicMock()
        body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="copy")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rest_mod.send_torrent_file_to_encoder(1, body, db=db))
        assert exc.value.status_code == 400
        assert "выключен" in str(exc.value.detail).lower()


def test_send_to_encoder_unknown_video_id(monkeypatch, tmp_path) -> None:
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
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="missing_video", audio_id="copy")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(9, body, db=db))
    assert exc.value.status_code == 400
    assert "video_id" in str(exc.value.detail)


def test_send_to_encoder_unknown_audio_id(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=10, full_path=str(media), info_hash="cc" * 20)

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="missing_audio")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(10, body, db=db))
    assert exc.value.status_code == 400
    assert "audio_id" in str(exc.value.detail)


def test_send_to_encoder_stereo_with_copy_rejected(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=13, full_path=str(media), info_hash="ff" * 20)

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(
        video_id="hevc_rip",
        audio_id="copy",
        audio_downmix="stereo",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(13, body, db=db))
    assert exc.value.status_code == 400
    assert "stereo" in str(exc.value.detail).lower()


def test_send_to_encoder_stereo_with_none_rejected(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=14, full_path=str(media), info_hash="11" * 20)
    audio_none = {
        "id": "none",
        "name": "None",
        "default": False,
        "codec": "none",
        "available": True,
    }

    async def fake_list_presets(_base_url: str):
        return _fake_layers(audios=[SAMPLE_AUDIO_COPY, audio_none, SAMPLE_AUDIO_OPUS])

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(
        video_id="hevc_rip",
        audio_id="none",
        audio_downmix="stereo",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(14, body, db=db))
    assert exc.value.status_code == 400
    assert "stereo" in str(exc.value.detail).lower()


def test_send_to_encoder_audio_unavailable(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(
        id=11,
        ui_status="ok",
        full_path=str(media),
        info_hash="dd" * 20,
    )

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="aac_fdk")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(11, body, db=db))
    assert exc.value.status_code == 400
    assert "недоступен" in str(exc.value.detail).lower()


def test_send_to_encoder_audio_available_string_false(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=12, full_path=str(media), info_hash="ee" * 20)
    audio = {**SAMPLE_AUDIO_OPUS, "id": "str_false", "available": "false"}

    async def fake_list_presets(_base_url: str):
        return _fake_layers(audios=[SAMPLE_AUDIO_COPY, audio])

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="str_false")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(12, body, db=db))
    assert exc.value.status_code == 400


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
    body = rest_mod.SendToEncoderIn(video_id="hevc_rip", audio_id="copy")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(3, body, db=db))
    assert exc.value.status_code == 404


def test_send_to_encoder_batch_empty_file_ids(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[],
        video_id="hevc_rip",
        audio_id="copy",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=MagicMock()))
    assert exc.value.status_code == 400
    assert "file_ids" in str(exc.value.detail)


def test_send_to_encoder_batch_size_limit(monkeypatch) -> None:
    from app.api import rest as rest_mod
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        rest_mod.SendToEncoderBatchIn(
            file_ids=list(range(1, ENCODE_BATCH_MAX_FILES + 2)),
            video_id="hevc_rip",
            audio_id="copy",
        )


def test_send_to_encoder_batch_happy_path(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media_a = tmp_path / "a.mkv"
    media_b = tmp_path / "b.mkv"
    media_a.write_bytes(b"a")
    media_b.write_bytes(b"b")
    rows = {
        1: SimpleNamespace(id=1, info_hash="aa" * 20, full_path=str(media_a)),
        2: SimpleNamespace(id=2, info_hash="aa" * 20, full_path=str(media_b)),
    }

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(
        base_url: str,
        *,
        paths: list[str],
        video_id: str,
        audio_id: str,
        audio_downmix: str = "none",
        **_kwargs,
    ):
        assert video_id == "hevc_rip"
        assert audio_id == "copy"
        assert audio_downmix == "none"
        assert paths == [str(media_a.resolve()), str(media_b.resolve())]
        return {
            "status_code": 200,
            "body": {"created": 2, "jobs": [{"id": "1"}, {"id": "2"}]},
        }

    def fake_resolve(row):
        return (tmp_path / ("a.mkv" if row.id == 1 else "b.mkv")).resolve()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", fake_resolve)
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.side_effect = lambda _cls, fid: rows.get(fid)
    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[1, 2],
        video_id="hevc_rip",
        audio_id="copy",
    )
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["ok"] is True
    assert data["created"] == 2
    assert data["file_ids"] == [1, 2]
    assert data["video_id"] == "hevc_rip"
    assert data["audio_id"] == "copy"
    assert data["local_errors"] is None
    assert "paths" not in data


def test_send_to_encoder_batch_local_fail_and_success(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ok.mkv"
    media.write_bytes(b"ok")
    rows = {
        1: SimpleNamespace(id=1, info_hash="aa" * 20, full_path=str(media)),
        2: SimpleNamespace(id=2, info_hash="aa" * 20, full_path=None),
    }

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    calls: list[dict] = []

    async def fake_encode(
        base_url: str,
        *,
        paths: list[str],
        video_id: str,
        audio_id: str,
        **_kwargs,
    ):
        calls.append({"paths": paths, "video_id": video_id, "audio_id": audio_id})
        # batch→1 path: энкодер отвечает как single (created: true bool)
        return {"status_code": 200, "body": {"created": True, "job_id": "1"}}

    def fake_resolve(row):
        if row.id == 2:
            return None
        return media.resolve()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", fake_resolve)
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.side_effect = lambda _cls, fid: rows.get(fid)
    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[1, 2],
        video_id="hevc_rip",
        audio_id="copy",
    )
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 207
    data = json.loads(response.body)
    assert data["partial"] is True
    assert data["ok"] is False
    assert data["file_ids"] == [1]
    assert data["local_errors"] == [{"file_id": 2, "error": "Файл недоступен для кодирования"}]
    assert "paths" not in data
    assert calls[0]["paths"] == [str(media.resolve())]
    assert data["created"] == 1
    assert isinstance(data["created"], int)


def test_send_to_encoder_batch_encoder_207(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media_a = tmp_path / "a.mkv"
    media_b = tmp_path / "b.mkv"
    media_a.write_bytes(b"a")
    media_b.write_bytes(b"b")
    rows = {
        1: SimpleNamespace(id=1, info_hash="aa" * 20),
        2: SimpleNamespace(id=2, info_hash="aa" * 20),
    }

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(base_url: str, *, paths: list[str], video_id: str, audio_id: str, **_kwargs):
        return {
            "status_code": 207,
            "body": {
                "created": 1,
                "jobs": [{"id": "1"}],
                "errors": [{"path": str(media_b.resolve()), "error": "bad"}],
            },
        }

    def fake_resolve(row):
        return (media_a if row.id == 1 else media_b).resolve()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", fake_resolve)
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.side_effect = lambda _cls, fid: rows.get(fid)
    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[1, 2],
        video_id="hevc_rip",
        audio_id="copy",
    )
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 207
    data = json.loads(response.body)
    assert data["partial"] is True
    assert data["ok"] is False
    assert data["created"] == 1
    assert data["file_ids"] == [1]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["file_id"] == 2
    assert "paths" not in data


def test_normalize_created_count_bool_and_fallback() -> None:
    from app.api import rest as rest_mod

    assert rest_mod._normalize_created_count({"created": True}, 200) == 1
    assert rest_mod._normalize_created_count({"created": False}, 200) == 0
    assert rest_mod._normalize_created_count({"created": 3}, 200) == 3
    assert rest_mod._normalize_created_count({"jobs": [{"id": "a"}, {"id": "b"}]}, 200) == 2
    assert rest_mod._normalize_created_count({"job_id": "x"}, 200) == 1
    assert rest_mod._normalize_created_count({"job": {"id": "x"}}, 200) == 1
    assert rest_mod._normalize_created_count({}, 200) == 0
    assert rest_mod._normalize_created_count({"created": "nope"}, 500) is None


def test_send_to_encoder_batch_all_local_fail(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: None)
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=1, info_hash="aa" * 20)
    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[1],
        video_id="hevc_rip",
        audio_id="copy",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail.get("message")


def test_send_to_encoder_batch_encoder_400(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod
    from app.services.video_kensetsu import VideoKensetsuHttpError

    media = tmp_path / "a.mkv"
    media.write_bytes(b"a")
    rows = {1: SimpleNamespace(id=1, info_hash="aa" * 20)}

    async def fake_list_presets(_base_url: str):
        return _fake_layers()

    async def fake_encode(base_url: str, *, paths: list[str], video_id: str, audio_id: str, **_kwargs):
        raise VideoKensetsuHttpError("нет путей", status_code=400)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.side_effect = lambda _cls, fid: rows.get(fid)
    body = rest_mod.SendToEncoderBatchIn(
        file_ids=[1],
        video_id="hevc_rip",
        audio_id="copy",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert exc.value.status_code == 400
    assert exc.value.detail == "нет путей"


def test_presets_endpoint_disabled(monkeypatch) -> None:
    from app.api import rest as rest_mod

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.video_kensetsu_presets(db=MagicMock()))
    assert exc.value.status_code == 400


def test_presets_endpoint_ok(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list(_base_url: str):
        return _fake_layers(
            audios=[
                SAMPLE_AUDIO_COPY,
                SAMPLE_AUDIO_FDK,
                {**SAMPLE_AUDIO_OPUS, "id": "zero", "name": "Zero", "available": 0},
                {**SAMPLE_AUDIO_OPUS, "id": "str_false", "name": "Str", "available": "false"},
            ]
        )

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list)
    payload = asyncio.run(rest_mod.video_kensetsu_presets(db=MagicMock()))
    assert payload["base_url"] == "http://encoder.test"
    assert "presets" not in payload
    assert payload["videos"][0]["id"] == "hevc_rip"
    assert payload["videos"][0]["name"] == "HEVC rip"
    assert payload["videos"][0]["crf"] == "23"
    assert payload["audios"][0]["id"] == "copy"
    assert payload["audios"][0]["available"] is True
    assert payload["audios"][1]["id"] == "aac_fdk"
    assert payload["audios"][1]["available"] is False
    assert payload["audios"][2]["available"] is False
    assert payload["audios"][3]["available"] is False
    assert payload["defaults"]["video_id"] == "hevc_rip"
    assert payload["defaults"]["audio_id"] == "copy"
    assert payload["defaults"]["audio_downmix"] == "none"


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
