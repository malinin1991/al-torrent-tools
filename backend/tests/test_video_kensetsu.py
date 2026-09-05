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
    coerce_preset_available,
    encode,
    encode_timeout_for_paths,
    find_preset,
    health,
    list_presets,
    video_kensetsu_ui_context,
)


SAMPLE_PRESET = {
    "id": "fast_encode",
    "name": "Быстрое кодирование",
    "description": "fast",
    "group": "rip",
    "available": True,
    "video_codec": "libx265",
    "preset": "fast",
    "crf": 28,
}


SAMPLE_PRESET_FDK = {
    "id": "aac_fdk_audio",
    "name": "HEVC + AAC FDK",
    "group": "rip",
    "available": False,
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
            preset_id="fast_encode",
        )
    )
    assert result["status_code"] == 200
    assert result["body"] == {"job_id": "j1", "created": True}
    assert captured["json"] == {
        "path": "/anilibria/2026/show/ep.mkv",
        "preset_id": "fast_encode",
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
            preset_id="bd_opus_audio",
        )
    )
    assert result["status_code"] == 207
    assert result["body"]["created"] == 1
    assert len(result["body"]["errors"]) == 1
    assert captured["json"] == {
        "paths": ["/anilibria/a.mkv", "/anilibria/b.mkv"],
        "preset_id": "bd_opus_audio",
    }


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
                preset_id="aac_fdk_audio",
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
            encode("http://encoder.test", paths=["/a.mkv"], preset_id="fast_encode")
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

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        assert base_url == "http://encoder.test"
        assert paths == [str(media)]
        assert preset_id == "fast_encode"
        return {"status_code": 200, "body": {"job_id": "ok"}}

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
    response = asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert response.status_code == 200
    result = json.loads(response.body)
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["preset_id"] == "fast_encode"
    assert result["path"] == str(media)
    assert result["result"] == {"job_id": "ok"}


def test_send_to_encoder_partial_207(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=7, full_path=str(media), info_hash="aa" * 20)

    async def fake_list_presets(_base_url: str):
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
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
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
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
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        raise VideoKensetsuHttpError("Пресет FDK недоступен", status_code=400)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
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
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        raise VideoKensetsuHttpError("путь вне корней", status_code=403)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(7, body, db=db))
    assert exc.value.status_code == 403


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


def test_send_to_encoder_preset_unavailable(monkeypatch, tmp_path) -> None:
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
        return [SAMPLE_PRESET_FDK]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="aac_fdk_audio")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(11, body, db=db))
    assert exc.value.status_code == 400
    assert "недоступен" in str(exc.value.detail).lower()


def test_send_to_encoder_preset_available_string_false(monkeypatch, tmp_path) -> None:
    from app.api import rest as rest_mod

    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    row = SimpleNamespace(id=12, full_path=str(media), info_hash="ee" * 20)
    preset = {**SAMPLE_PRESET, "id": "str_false", "available": "false"}

    async def fake_list_presets(_base_url: str):
        return [preset]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = row
    body = rest_mod.SendToEncoderIn(preset_id="str_false")
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
    body = rest_mod.SendToEncoderIn(preset_id="fast_encode")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest_mod.send_torrent_file_to_encoder(3, body, db=db))
    assert exc.value.status_code == 404


def test_send_to_encoder_batch_empty_file_ids(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list_presets(_base_url: str):
        return [SAMPLE_PRESET]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    body = rest_mod.SendToEncoderBatchIn(file_ids=[], preset_id="fast_encode")
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
            preset_id="fast_encode",
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
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        assert preset_id == "fast_encode"
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
    body = rest_mod.SendToEncoderBatchIn(file_ids=[1, 2], preset_id="fast_encode")
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["ok"] is True
    assert data["created"] == 2
    assert data["file_ids"] == [1, 2]
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
        return [SAMPLE_PRESET]

    calls: list[dict] = []

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        calls.append({"paths": paths, "preset_id": preset_id})
        return {"status_code": 200, "body": {"created": 1, "jobs": [{"id": "1"}]}}

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
    body = rest_mod.SendToEncoderBatchIn(file_ids=[1, 2], preset_id="fast_encode")
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 207
    data = json.loads(response.body)
    assert data["partial"] is True
    assert data["file_ids"] == [1]
    assert data["local_errors"] == [{"file_id": 2, "error": "Файл недоступен для кодирования"}]
    assert "paths" not in data
    assert calls[0]["paths"] == [str(media.resolve())]


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
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
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
    body = rest_mod.SendToEncoderBatchIn(file_ids=[1, 2], preset_id="fast_encode")
    response = asyncio.run(rest_mod.send_torrent_files_to_encoder_batch(body, db=db))
    assert response.status_code == 207
    data = json.loads(response.body)
    assert data["partial"] is True
    assert data["created"] == 1
    assert data["file_ids"] == [1]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["file_id"] == 2
    assert "paths" not in data


def test_send_to_encoder_batch_all_local_fail(monkeypatch) -> None:
    from app.api import rest as rest_mod

    async def fake_list_presets(_base_url: str):
        return [SAMPLE_PRESET]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: None)
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)

    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=1, info_hash="aa" * 20)
    body = rest_mod.SendToEncoderBatchIn(file_ids=[1], preset_id="fast_encode")
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
        return [SAMPLE_PRESET]

    async def fake_encode(base_url: str, *, paths: list[str], preset_id: str, **_kwargs):
        raise VideoKensetsuHttpError("нет путей", status_code=400)

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "torrent_allows_media_download", lambda _db, _h: True)
    monkeypatch.setattr(rest_mod, "resolve_media_file_for_download", lambda _row: media.resolve())
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list_presets)
    monkeypatch.setattr(rest_mod, "video_kensetsu_encode", fake_encode)

    db = MagicMock()
    db.get.side_effect = lambda _cls, fid: rows.get(fid)
    body = rest_mod.SendToEncoderBatchIn(file_ids=[1], preset_id="fast_encode")
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
        return [
            SAMPLE_PRESET,
            SAMPLE_PRESET_FDK,
            {**SAMPLE_PRESET, "id": "zero", "name": "Zero", "available": 0},
            {**SAMPLE_PRESET, "id": "str_false", "name": "Str", "available": "false"},
        ]

    monkeypatch.setattr(rest_mod, "is_video_kensetsu_enabled", lambda _db: True)
    monkeypatch.setattr(rest_mod, "resolve_video_kensetsu_base_url", lambda _db: "http://encoder.test")
    monkeypatch.setattr(rest_mod, "video_kensetsu_list_presets", fake_list)
    payload = asyncio.run(rest_mod.video_kensetsu_presets(db=MagicMock()))
    assert payload["base_url"] == "http://encoder.test"
    assert payload["presets"][0]["id"] == "fast_encode"
    assert payload["presets"][0]["name"] == "Быстрое кодирование"
    assert payload["presets"][0]["group"] == "rip"
    assert payload["presets"][0]["available"] is True
    assert payload["presets"][1]["id"] == "aac_fdk_audio"
    assert payload["presets"][1]["available"] is False
    assert payload["presets"][2]["available"] is False
    assert payload["presets"][3]["available"] is False


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
