from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.pipeline import TorrentPipelineService


def _pipeline(*, status: str, pipeline_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=pipeline_id,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=status,
        error=None,
        master_added_at=None,
        slave_added_at=None,
    )


def _sample_torrent_bytes() -> bytes:
    announce = b"http://tr.libria.fun:2710/announce"
    info = b"d4:name4:test6:lengthi1ee"
    return b"d8:announce" + f"{len(announce)}:".encode() + announce + b"4:info" + info + b"e"


def test_process_completion_noop_for_done() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_DONE)
    service._claim_master_complete = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent-bytes")

    assert result.status == TorrentPipelineService.STATUS_DONE
    service._claim_master_complete.assert_not_called()


def test_process_completion_noop_for_slave_added() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_SLAVE_ADDED)
    service._claim_master_complete = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent-bytes")

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    service._claim_master_complete.assert_not_called()


def test_process_completion_skip_when_claim_loses_race() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED)

    def refresh(obj: SimpleNamespace) -> None:
        # После проигранного claim статус уже done (другой воркер/webhook).
        obj.status = TorrentPipelineService.STATUS_DONE

    db.refresh.side_effect = refresh
    service._claim_master_complete = MagicMock(return_value=None)  # type: ignore[method-assign]
    service._get_qb_client = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent-bytes")

    assert result.status == TorrentPipelineService.STATUS_DONE
    service._get_qb_client.assert_not_called()


def test_process_completion_rejects_failed_status() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_FAILED)
    service._claim_master_complete = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="нельзя завершить"):
        service.process_completion(pipeline, b"torrent-bytes")


def test_process_completion_rejects_discovered_status() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_DISCOVERED)
    service._claim_master_complete = MagicMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="нельзя завершить"):
        service.process_completion(pipeline, b"torrent-bytes")


def test_get_latest_by_hash_prefers_non_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    active = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED, pipeline_id=2)
    db.scalar.side_effect = [active]

    result = service.get_latest_by_hash("ABC123")

    assert result is active
    assert result.status == TorrentPipelineService.STATUS_MASTER_ADDED
    assert db.scalar.call_count == 1


def test_process_completion_happy_path_adds_to_slave(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED)
    claimed = _pipeline(status=TorrentPipelineService.STATUS_MASTER_COMPLETE)

    service._claim_master_complete = MagicMock(return_value=claimed)  # type: ignore[method-assign]
    service._enqueue_hash_torrent = MagicMock()  # type: ignore[method-assign]
    slave = SimpleNamespace(
        host="slave.local",
        port=8080,
        username="u",
        password_encrypted="p",
    )
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(  # type: ignore[method-assign]
        return_value=("Name / Orig (1-2) [HEVC]", "https://www.anilibria.top/anime/releases/release/x/torrents", "winter.2024", ["Комедия"])
    )

    qb = MagicMock()
    comment_url = "https://www.anilibria.top/anime/releases/release/x/torrents"
    from app.services.qbittorrent import torrent_info_hash

    info_hash = torrent_info_hash(_sample_torrent_bytes())
    present = MagicMock(hash=info_hash, infohash_v1=info_hash, infohash_v2=None)
    qb.torrents_info.return_value = [present]
    props = MagicMock()
    props.comment = comment_url
    qb.torrents_properties.return_value = props
    fake_client_cls = MagicMock(return_value=qb)
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", fake_client_cls)
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "testpk")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)
    monkeypatch.setattr("app.services.qbittorrent.time.sleep", lambda *_: None)

    def mark_slave(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    def mark_done(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_DONE
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock(side_effect=mark_done)  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_DONE
    qb.auth_log_in.assert_called_once()
    qb.torrents_add.assert_called_once()
    assert qb.torrents_add.call_args.kwargs.get("rename") == "Name / Orig (1-2) [HEVC]"
    assert qb.torrents_set_comment.called


def test_process_completion_resumes_master_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_COMPLETE)

    service._claim_master_complete = MagicMock()  # type: ignore[method-assign]
    service._enqueue_hash_torrent = MagicMock()  # type: ignore[method-assign]
    slave = SimpleNamespace(host="slave.local", port=8080, username="u", password_encrypted="p")
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(return_value=(None, None, None, []))  # type: ignore[method-assign]
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)
    monkeypatch.setattr("app.services.pipeline.qb_add_torrent", MagicMock(return_value=(True, True, True)))

    def mark_slave(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    def mark_done(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_DONE
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock(side_effect=mark_done)  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_DONE
    service._claim_master_complete.assert_not_called()


def test_classify_master_torrent_states() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED)

    qb = MagicMock()
    service._get_master_api = MagicMock(return_value=qb)  # type: ignore[method-assign]

    qb.torrents_info.return_value = []
    assert service.classify_master_torrent(pipeline) == "missing"

    qb.torrents_info.return_value = [SimpleNamespace(progress=0.4, state="downloading")]
    assert service.classify_master_torrent(pipeline) == "in_progress"

    qb.torrents_info.return_value = [SimpleNamespace(progress=1.0, state="uploading")]
    assert service.classify_master_torrent(pipeline) == "complete"

    qb.torrents_info.return_value = [SimpleNamespace(progress=0.0, state="pausedUP")]
    assert service.classify_master_torrent(pipeline) == "complete"


def test_reconcile_with_master_actions() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    waiting = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED, pipeline_id=1)
    missing = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED, pipeline_id=2)
    complete = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED, pipeline_id=3)
    service.get_pipelines_awaiting_slave = MagicMock(  # type: ignore[method-assign]
        return_value=[waiting, missing, complete]
    )
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[])  # type: ignore[method-assign]

    def classify(p: SimpleNamespace) -> str:
        return {1: "in_progress", 2: "missing", 3: "complete"}[p.id]

    service.classify_master_torrent = MagicMock(side_effect=classify)  # type: ignore[method-assign]
    service.mark_cancelled = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda p, reason: setattr(p, "status", TorrentPipelineService.STATUS_CANCELLED) or p
    )
    service.process_completion = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda p, data: setattr(p, "status", TorrentPipelineService.STATUS_DONE) or p
    )
    service.mark_failed = MagicMock()  # type: ignore[method-assign]

    stats = service.reconcile_with_master(load_torrent_bytes=lambda p: b"torrent")

    assert stats["checked"] == 3
    assert stats["waiting"] == 1
    assert stats["cancelled"] == 1
    assert stats["sent_to_slave"] == 1
    assert stats["errors"] == 0
    service.mark_cancelled.assert_called_once()
    service.process_completion.assert_called_once()
    service.mark_failed.assert_not_called()


def test_reconcile_connection_error_does_not_mark_failed() -> None:
    from qbittorrentapi.exceptions import APIConnectionError

    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED, pipeline_id=7)
    service.get_pipelines_awaiting_slave = MagicMock(return_value=[pipeline])  # type: ignore[method-assign]
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[])  # type: ignore[method-assign]
    service.classify_master_torrent = MagicMock(  # type: ignore[method-assign]
        side_effect=APIConnectionError("Connection refused")
    )
    service.mark_failed = MagicMock()  # type: ignore[method-assign]

    stats = service.reconcile_with_master(load_torrent_bytes=lambda p: b"torrent")

    assert stats["errors"] == 0
    assert stats["waiting"] == 1
    assert pipeline.status == TorrentPipelineService.STATUS_MASTER_ADDED
    service.mark_failed.assert_not_called()


def test_reconcile_recovers_failed_when_seeding_on_master() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    failed = _pipeline(status=TorrentPipelineService.STATUS_FAILED, pipeline_id=9)
    failed.error = "Master недоступен: Connection refused"
    service.get_pipelines_awaiting_slave = MagicMock(return_value=[])  # type: ignore[method-assign]
    service.get_failed_qb_wait_pipelines = MagicMock(return_value=[failed])  # type: ignore[method-assign]
    service.classify_master_torrent = MagicMock(return_value="complete")  # type: ignore[method-assign]

    def mark_added(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_MASTER_ADDED
        p.error = None
        return p

    service.mark_master_added = MagicMock(side_effect=mark_added)  # type: ignore[method-assign]
    service.process_completion = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda p, data: setattr(p, "status", TorrentPipelineService.STATUS_DONE) or p
    )
    service.mark_failed = MagicMock()  # type: ignore[method-assign]

    stats = service.reconcile_with_master(load_torrent_bytes=lambda p: b"torrent")

    assert stats["recovered"] == 1
    assert stats["sent_to_slave"] == 1
    service.mark_master_added.assert_called_once()
    service.process_completion.assert_called_once()
    service.mark_failed.assert_not_called()


def test_process_completion_slave_auth_error_goes_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    from qbittorrentapi.exceptions import LoginFailed

    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_COMPLETE)

    service._enqueue_hash_torrent = MagicMock()  # type: ignore[method-assign]
    slave = SimpleNamespace(host="slave.local", port=8080, username="u", password_encrypted="p")
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(return_value=(None, None, None, []))  # type: ignore[method-assign]

    qb = MagicMock()
    qb.auth_log_in.side_effect = LoginFailed("bad password")
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", MagicMock(return_value=qb))
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)

    def mark_waiting(p: SimpleNamespace, reason: str | None = None) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_WAITING_SLAVE
        p.error = reason
        return p

    service.mark_waiting_slave = MagicMock(side_effect=mark_waiting)  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_WAITING_SLAVE
    assert result.error is not None
    assert "Настройки" in result.error
    assert "пароль" in result.error.lower() or "авторизац" in result.error.lower()


def test_process_completion_slave_unavailable_goes_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    from qbittorrentapi.exceptions import APIConnectionError

    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED)
    claimed = _pipeline(status=TorrentPipelineService.STATUS_MASTER_COMPLETE)

    service._claim_master_complete = MagicMock(return_value=claimed)  # type: ignore[method-assign]
    service._enqueue_hash_torrent = MagicMock()  # type: ignore[method-assign]
    slave = SimpleNamespace(host="slave.local", port=8080, username="u", password_encrypted="p")
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(return_value=(None, None, None, []))  # type: ignore[method-assign]

    qb = MagicMock()
    qb.auth_log_in.side_effect = APIConnectionError("connection refused")
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", MagicMock(return_value=qb))
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)

    def mark_waiting(p: SimpleNamespace, reason: str | None = None) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_WAITING_SLAVE
        p.error = reason
        return p

    service.mark_waiting_slave = MagicMock(side_effect=mark_waiting)  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_WAITING_SLAVE
    service.mark_waiting_slave.assert_called_once()


def test_process_completion_conflict_on_slave_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from qbittorrentapi.exceptions import Conflict409Error

    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_MASTER_ADDED)
    claimed = _pipeline(status=TorrentPipelineService.STATUS_MASTER_COMPLETE)

    service._claim_master_complete = MagicMock(return_value=claimed)  # type: ignore[method-assign]
    service._enqueue_hash_torrent = MagicMock()  # type: ignore[method-assign]
    slave = SimpleNamespace(
        host="slave.local",
        port=8080,
        username="u",
        password_encrypted="p",
    )
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(return_value=(None, None, None, []))  # type: ignore[method-assign]

    qb = MagicMock()
    qb.torrents_add.side_effect = Conflict409Error("Conflict")
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", MagicMock(return_value=qb))
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)

    def mark_slave(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    def mark_done(p: SimpleNamespace) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_DONE
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock(side_effect=mark_done)  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_DONE
    service.mark_slave_added.assert_called_once()
    service.mark_done.assert_called_once()
