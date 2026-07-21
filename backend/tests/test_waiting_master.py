import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from qbittorrentapi.exceptions import APIConnectionError, LoginFailed

from app.jobs.waiting_master_retry import retry_waiting_master_pipelines
from app.providers.anilibria.client import AniLibriaClient
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import is_qb_auth_error, is_qb_unavailable, should_wait_for_qb


def test_is_qb_unavailable() -> None:
    assert is_qb_unavailable(APIConnectionError("connection refused")) is True
    assert is_qb_unavailable(ConnectionError("boom")) is True
    assert is_qb_unavailable(RuntimeError("Connection refused to host")) is True
    assert is_qb_unavailable(LoginFailed("bad password")) is False
    assert is_qb_unavailable(RuntimeError("Conflict")) is False


def test_should_wait_for_qb_includes_auth() -> None:
    assert should_wait_for_qb(LoginFailed("bad password")) is True
    assert is_qb_auth_error(LoginFailed("bad password")) is True
    assert is_qb_auth_error(RuntimeError("Ошибка авторизации: wrong")) is True
    assert should_wait_for_qb(APIConnectionError("down")) is True
    assert should_wait_for_qb(RuntimeError("Conflict")) is False


def test_is_qb_wait_error_text() -> None:
    from app.services.qbittorrent import is_qb_wait_error_text

    assert is_qb_wait_error_text("Master недоступен: Connection refused") is True
    assert is_qb_wait_error_text("Reconcile: connection refused") is True
    assert is_qb_wait_error_text("неверный hash") is False
    assert is_qb_wait_error_text(None) is False
    assert is_qb_wait_error_text("") is False


def test_poll_master_pipeline_connection_error_does_not_mark_failed(monkeypatch) -> None:
    """Connection error при poll не переводит master_added → failed."""
    from worker import _poll_master_pipeline

    db = MagicMock()
    pipeline = SimpleNamespace(
        id=11,
        info_hash="pollhash",
        torrent_id=1,
        status=TorrentPipelineService.STATUS_MASTER_ADDED,
        error=None,
        master_added_at=None,
    )
    service = TorrentPipelineService(db)
    service.get_master_added_older_than = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service.classify_master_torrent = MagicMock(  # type: ignore[method-assign]
        side_effect=APIConnectionError("Connection refused")
    )
    service.mark_failed = MagicMock()  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]
    service.process_completion = MagicMock()  # type: ignore[method-assign]

    session_cm = MagicMock()
    session_cm.__enter__.return_value = db
    session_cm.__exit__.return_value = False
    monkeypatch.setattr("worker.SessionLocal", lambda: session_cm)
    monkeypatch.setattr("worker.TorrentPipelineService", lambda db: service)
    monkeypatch.setattr("worker._setting_int", lambda *a, **k: 5)

    asyncio.run(_poll_master_pipeline())

    assert pipeline.status == TorrentPipelineService.STATUS_MASTER_ADDED
    service.mark_failed.assert_not_called()
    service.process_completion.assert_not_called()


def test_get_torrent_returns_none_on_404(monkeypatch) -> None:
    client = AniLibriaClient(base_url="https://example.test/api/v1", request_retries=1)

    response = MagicMock()
    response.status_code = 404
    http_error = httpx.HTTPStatusError("404", request=MagicMock(), response=response)

    async def boom(*args, **kwargs):
        raise RuntimeError("AniLibria API недоступен") from http_error

    monkeypatch.setattr(client, "_request_json", boom)
    assert asyncio.run(client.get_torrent("abc")) is None
    assert asyncio.run(client.torrent_exists("abc")) is False


def test_retry_recovers_failed_when_already_on_master(monkeypatch) -> None:
    db = MagicMock()
    pipeline = SimpleNamespace(
        id=55,
        info_hash="recov",
        torrent_id=7,
        status=TorrentPipelineService.STATUS_FAILED,
        error="Master недоступен: Connection refused",
    )
    service = TorrentPipelineService(db)
    service.get_waiting_master_pipelines = MagicMock(return_value=[])  # type: ignore[method-assign]
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service._get_qb_client = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(host="h", port=1, username="u", password_encrypted="p")
    )
    service.classify_master_torrent = MagicMock(return_value="complete")  # type: ignore[method-assign]
    service.load_torrent_bytes_from_archive = MagicMock(return_value=b"torrent")  # type: ignore[method-assign]

    def mark_added(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_MASTER_ADDED
        p.error = None
        return p

    service.mark_master_added = MagicMock(side_effect=mark_added)  # type: ignore[method-assign]
    done = SimpleNamespace(status=TorrentPipelineService.STATUS_DONE, error=None)
    service.process_completion = MagicMock(return_value=done)  # type: ignore[method-assign]
    service.mark_failed = MagicMock()  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.TorrentPipelineService",
        lambda db, job_id=None: service,
    )
    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.test_qb_connection",
        lambda **kwargs: {"ok": True},
    )
    monkeypatch.setattr("app.jobs.waiting_master_retry.qbittorrentapi.Client", MagicMock(return_value=MagicMock()))
    al = MagicMock()
    al.passkey = ""
    monkeypatch.setattr("app.jobs.waiting_master_retry.build_anilibria_client", lambda db: al)
    monkeypatch.setattr("app.jobs.waiting_master_retry.ensure_passkey_stored", AsyncMock(return_value=None))

    stats = asyncio.run(retry_waiting_master_pipelines(db))

    assert stats["recovered"] == 1
    assert stats["submitted"] == 1
    service.mark_master_added.assert_called_once()
    service.process_completion.assert_called_once()
    service.mark_failed.assert_not_called()


def test_retry_waiting_master_cancels_missing_api(monkeypatch) -> None:
    db = MagicMock()
    pipeline = SimpleNamespace(
        id=1,
        info_hash="abc123",
        torrent_id=42,
        status=TorrentPipelineService.STATUS_WAITING_MASTER,
        error=None,
    )

    service = TorrentPipelineService(db)
    service.get_waiting_master_pipelines = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[])  # type: ignore[method-assign]
    service._get_qb_client = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(host="h", port=1, username="u", password_encrypted="p")
    )
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]
    service.mark_master_added = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.TorrentPipelineService",
        lambda db, job_id=None: service,
    )
    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.test_qb_connection",
        lambda **kwargs: {"ok": True},
    )
    qb = MagicMock()
    monkeypatch.setattr("app.jobs.waiting_master_retry.qbittorrentapi.Client", MagicMock(return_value=qb))
    al = MagicMock()
    al.passkey = ""
    al.torrent_exists = AsyncMock(return_value=False)
    monkeypatch.setattr("app.jobs.waiting_master_retry.build_anilibria_client", lambda db: al)
    monkeypatch.setattr("app.jobs.waiting_master_retry.ensure_passkey_stored", AsyncMock(return_value=None))

    stats = asyncio.run(retry_waiting_master_pipelines(db))

    assert stats["master_up"] is True
    assert stats["cancelled"] == 1
    assert stats["submitted"] == 0
    service.mark_cancelled.assert_called_once()
    service.mark_master_added.assert_not_called()


def test_retry_waiting_master_submits_when_api_ok(monkeypatch) -> None:
    db = MagicMock()
    pipeline = SimpleNamespace(
        id=2,
        info_hash="def456",
        torrent_id=99,
        status=TorrentPipelineService.STATUS_WAITING_MASTER,
        error=None,
    )

    service = TorrentPipelineService(db)
    service.get_waiting_master_pipelines = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[])  # type: ignore[method-assign]
    service._get_qb_client = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(host="h", port=1, username="u", password_encrypted="p")
    )
    service.load_torrent_bytes_from_archive = MagicMock(return_value=b"torrent")  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(return_value=(None, None, None, []))  # type: ignore[method-assign]
    service.mark_master_added = MagicMock()  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.TorrentPipelineService",
        lambda db, job_id=None: service,
    )
    monkeypatch.setattr(
        "app.jobs.waiting_master_retry.test_qb_connection",
        lambda **kwargs: {"ok": True},
    )
    qb = MagicMock()
    monkeypatch.setattr("app.jobs.waiting_master_retry.qbittorrentapi.Client", MagicMock(return_value=qb))
    monkeypatch.setattr("app.jobs.waiting_master_retry.qb_add_torrent", MagicMock(return_value=(True, True, True)))
    al = MagicMock()
    al.passkey = ""
    al.torrent_exists = AsyncMock(side_effect=[True])
    monkeypatch.setattr("app.jobs.waiting_master_retry.build_anilibria_client", lambda db: al)
    monkeypatch.setattr("app.jobs.waiting_master_retry.ensure_passkey_stored", AsyncMock(return_value=None))

    stats = asyncio.run(retry_waiting_master_pipelines(db))

    assert stats["submitted"] == 1
    assert stats["cancelled"] == 0
    service.mark_master_added.assert_called_once()


def test_retry_waiting_slave_cancels_missing_master(monkeypatch) -> None:
    from app.jobs.waiting_slave_retry import retry_waiting_slave_pipelines

    db = MagicMock()
    pipeline = SimpleNamespace(
        id=3,
        info_hash="aaa",
        torrent_id=1,
        status=TorrentPipelineService.STATUS_WAITING_SLAVE,
        error=None,
    )
    service = TorrentPipelineService(db)
    service.get_waiting_slave_pipelines = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service._get_qb_client = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(host="h", port=1, username="u", password_encrypted="p")
    )
    service.classify_master_torrent = MagicMock(return_value="missing")  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]
    service.process_completion = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.TorrentPipelineService",
        lambda db, job_id=None: service,
    )
    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.test_qb_connection",
        lambda **kwargs: {"ok": True},
    )
    al = MagicMock()
    al.passkey = ""
    monkeypatch.setattr("app.jobs.waiting_slave_retry.build_anilibria_client", lambda db: al)
    monkeypatch.setattr("app.jobs.waiting_slave_retry.ensure_passkey_stored", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.torrent_still_in_api",
        AsyncMock(return_value=True),
    )

    stats = asyncio.run(retry_waiting_slave_pipelines(db))

    assert stats["cancelled"] == 1
    assert stats["submitted"] == 0
    service.mark_cancelled.assert_called_once()
    service.process_completion.assert_not_called()


def test_retry_waiting_slave_submits_when_ready(monkeypatch) -> None:
    from app.jobs.waiting_slave_retry import retry_waiting_slave_pipelines

    db = MagicMock()
    pipeline = SimpleNamespace(
        id=4,
        info_hash="bbb",
        torrent_id=2,
        status=TorrentPipelineService.STATUS_WAITING_SLAVE,
        error=None,
    )
    service = TorrentPipelineService(db)
    service.get_waiting_slave_pipelines = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service._get_qb_client = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(host="h", port=1, username="u", password_encrypted="p")
    )
    service.classify_master_torrent = MagicMock(return_value="complete")  # type: ignore[method-assign]
    service.load_torrent_bytes_from_archive = MagicMock(return_value=b"torrent")  # type: ignore[method-assign]
    done = SimpleNamespace(status=TorrentPipelineService.STATUS_DONE, error=None)
    service.process_completion = MagicMock(return_value=done)  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.TorrentPipelineService",
        lambda db, job_id=None: service,
    )
    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.test_qb_connection",
        lambda **kwargs: {"ok": True},
    )
    al = MagicMock()
    al.passkey = ""
    monkeypatch.setattr("app.jobs.waiting_slave_retry.build_anilibria_client", lambda db: al)
    monkeypatch.setattr("app.jobs.waiting_slave_retry.ensure_passkey_stored", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.jobs.waiting_slave_retry.torrent_still_in_api",
        AsyncMock(return_value=True),
    )

    stats = asyncio.run(retry_waiting_slave_pipelines(db))

    assert stats["submitted"] == 1
    service.process_completion.assert_called_once()
