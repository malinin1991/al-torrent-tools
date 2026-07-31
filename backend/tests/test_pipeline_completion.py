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
        slave_completed_at=None,
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
        name="slave-qb-label",
    )
    service._get_qb_client = MagicMock(return_value=slave)  # type: ignore[method-assign]
    service._resolve_qb_meta = MagicMock(  # type: ignore[method-assign]
        return_value=("Name / Orig (1-2) [HEVC]", "https://www.anilibria.top/anime/releases/release/x/torrents", "winter.2024", ["Комедия"])
    )

    qb = MagicMock()
    comment_url = "https://www.anilibria.top/anime/releases/release/x/torrents"
    from app.services.qbittorrent import torrent_info_hash

    info_hash = torrent_info_hash(_sample_torrent_bytes())
    present = MagicMock(hash=info_hash, infohash_v1=info_hash, infohash_v2=None, tags="")
    qb.torrents_info.return_value = [present]
    comment_state = {"value": "comment-from-torrent"}

    def _properties(**_kwargs):
        props = MagicMock()
        props.comment = comment_state["value"]
        return props

    def _set_comment(**kwargs):
        comment_state["value"] = kwargs.get("comment", "")

    def _add_tags(**kwargs):
        tags = kwargs.get("tags") or []
        present.tags = ",".join(tags)

    qb.torrents_properties.side_effect = _properties
    qb.torrents_set_comment.side_effect = _set_comment
    qb.torrents_add_tags.side_effect = _add_tags
    fake_client_cls = MagicMock(return_value=qb)
    monkeypatch.setattr("app.services.pipeline.qbittorrentapi.Client", fake_client_cls)
    monkeypatch.setattr("app.services.pipeline.get_setting_value", lambda *a, **k: "testpk")
    monkeypatch.setattr("app.services.pipeline.ensure_announce_passkey", lambda data, pk: data)
    monkeypatch.setattr("app.services.qbittorrent.time.sleep", lambda *_: None)

    def mark_slave(p: SimpleNamespace, **_kwargs) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    qb.auth_log_in.assert_called_once()
    qb.torrents_add.assert_called_once()
    assert qb.torrents_add.call_args.kwargs.get("rename") == "Name / Orig (1-2) [HEVC]"
    assert qb.torrents_set_comment.called
    assert comment_state["value"] == comment_url
    service.mark_slave_added.assert_called_once()
    service.mark_done.assert_not_called()
    slave_details = service.mark_slave_added.call_args.kwargs.get("details") or {}
    assert slave_details.get("qb_name") == "Name / Orig (1-2) [HEVC]"
    assert slave_details.get("qb_name") != "slave-qb-label"


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

    def mark_slave(p: SimpleNamespace, **_kwargs) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    service.mark_done.assert_not_called()
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
        side_effect=lambda p, data: setattr(p, "status", TorrentPipelineService.STATUS_SLAVE_ADDED) or p
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
        side_effect=lambda p, data: setattr(p, "status", TorrentPipelineService.STATUS_SLAVE_ADDED) or p
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

    def mark_slave(p: SimpleNamespace, **_kwargs) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return p

    service.mark_slave_added = MagicMock(side_effect=mark_slave)  # type: ignore[method-assign]
    service.mark_done = MagicMock()  # type: ignore[method-assign]

    result = service.process_completion(pipeline, _sample_torrent_bytes())

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    service.mark_slave_added.assert_called_once()
    service.mark_done.assert_not_called()


def test_process_slave_completion_marks_done() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_SLAVE_ADDED)
    service.classify_slave_torrent = MagicMock(return_value="complete")  # type: ignore[method-assign]

    def mark_done(p: SimpleNamespace, **_kwargs) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_DONE
        return p

    service.mark_done = MagicMock(side_effect=mark_done)  # type: ignore[method-assign]

    result = service.process_slave_completion(pipeline)

    assert result.status == TorrentPipelineService.STATUS_DONE
    service.mark_done.assert_called_once()


def test_process_slave_completion_in_progress_noop() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_SLAVE_ADDED)
    service.classify_slave_torrent = MagicMock(return_value="in_progress")  # type: ignore[method-assign]
    service.mark_done = MagicMock()  # type: ignore[method-assign]

    result = service.process_slave_completion(pipeline)

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    service.mark_done.assert_not_called()


def test_process_slave_completion_missing_cancels() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_SLAVE_ADDED)
    service.classify_slave_torrent = MagicMock(return_value="missing")  # type: ignore[method-assign]

    def mark_cancelled(p: SimpleNamespace, reason: str) -> SimpleNamespace:
        p.status = TorrentPipelineService.STATUS_CANCELLED
        p.error = reason
        return p

    service.mark_cancelled = MagicMock(side_effect=mark_cancelled)  # type: ignore[method-assign]

    result = service.process_slave_completion(pipeline)

    assert result.status == TorrentPipelineService.STATUS_CANCELLED
    service.mark_cancelled.assert_called_once()


def test_classify_slave_torrent_states() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = _pipeline(status=TorrentPipelineService.STATUS_SLAVE_ADDED)

    qb = MagicMock()
    service._get_slave_api = MagicMock(return_value=qb)  # type: ignore[method-assign]

    qb.torrents_info.return_value = []
    assert service.classify_slave_torrent(pipeline) == "missing"

    qb.torrents_info.return_value = [SimpleNamespace(progress=0.4, state="downloading")]
    assert service.classify_slave_torrent(pipeline) == "in_progress"

    qb.torrents_info.return_value = [SimpleNamespace(progress=1.0, state="uploading")]
    assert service.classify_slave_torrent(pipeline) == "complete"


def test_pipeline_ci_stages_mapping() -> None:
    from app.services.pipeline import pipeline_ci_stages

    # discover → master → tg → slave → done → Δtg
    slave_added = pipeline_ci_stages("slave_added", tg_status="queued", files_status="running")
    assert [s["id"] for s in slave_added] == [
        "discover",
        "master",
        "tg",
        "slave",
        "done",
        "files",
    ]
    assert [s["label"] for s in slave_added][-1] == "Δtg"
    assert [s["state"] for s in slave_added] == [
        "success",
        "success",
        "running",
        "running",
        "pending",
        "running",
    ]

    master_complete = pipeline_ci_stages("master_complete", tg_status="sent")
    assert [s["state"] for s in master_complete] == [
        "success",
        "success",
        "success",
        "running",
        "pending",
        "pending",
    ]

    master_added = pipeline_ci_stages("master_added", tg_status="queued")
    assert [s["state"] for s in master_added] == [
        "success",
        "running",
        "running",
        "pending",
        "pending",
        "pending",
    ]

    done = pipeline_ci_stages("done", tg_status="sent", files_status="success")
    assert [s["state"] for s in done] == ["success"] * 6

    done_hash_running = pipeline_ci_stages("done", tg_status="skipped", files_status="running")
    assert done_hash_running[2]["state"] == "success"  # tg skipped
    assert done_hash_running[5]["state"] == "running"  # Δtg

    failed = pipeline_ci_stages("failed", master_added_at="t")
    assert failed[0]["state"] == "success"
    assert failed[1]["state"] == "failed"
    assert failed[3]["state"] == "pending"

    failed_slave = pipeline_ci_stages(
        "failed",
        master_added_at="t",
        error="Slave недоступен — waiting_slave",
    )
    assert failed_slave[1]["state"] == "success"
    assert failed_slave[3]["state"] == "failed"


def test_resolve_files_stage_statuses() -> None:
    from app.services.pipeline import resolve_files_stage_statuses

    db = MagicMock()
    p_done = SimpleNamespace(id=1, status="done", slave_added_at="t")
    p_fail = SimpleNamespace(id=2, status="failed", slave_added_at="t")
    p_early = SimpleNamespace(id=3, status="master_added", slave_added_at=None)

    # Последние hash-события: done→hash_done, failed без событий
    db.execute.return_value.all.return_value = [
        (1, "hash_done", 10),
        (1, "hash_progress", 9),
    ]
    result = resolve_files_stage_statuses(db, [p_done, p_fail, p_early])
    assert result[1] == "success"
    assert result[2] == "pending"  # failed без hash — не running
    assert result[3] == "pending"
