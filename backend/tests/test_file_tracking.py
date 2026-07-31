"""Тесты file tracking: парсер .torrent, gate BLAKE3, api_present, enqueue, TG, UI."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.db.models import PipelineEvent
from app.services.file_hasher import upsert_disk_hash
from app.services.file_tracker import FileChange, FileTrackerService, mark_missing_api_present_false, update_api_present_for_release
from app.services.pipeline import TorrentPipelineService
from app.services.releases_view import ReleaseTorrentRow, split_active_archived
from app.services.telegram_notify import build_file_changes_notification_text
from app.services.torrent_files_meta import parse_torrent_file_list


def _encode_str(value: str) -> bytes:
    raw = value.encode("utf-8")
    return f"{len(raw)}:".encode() + raw


def _multi_file_torrent_bytes() -> bytes:
    """Минимальный multi-file .torrent с двумя файлами."""
    # info = {name, files: [{length, path}, ...]}
    name = _encode_str("Show Name")
    f1_path = b"l" + _encode_str("ep01.mkv") + b"e"
    f1 = b"d6:lengthi100e4:path" + f1_path + b"e"
    f2_path = b"l" + _encode_str("ep02.mkv") + b"e"
    f2 = b"d6:lengthi200e4:path" + f2_path + b"e"
    files = b"l" + f1 + f2 + b"e"
    info = b"d4:name" + name + b"5:files" + files + b"e"
    announce = _encode_str("http://tr.libria.fun:2710/announce")
    return b"d8:announce" + announce + b"4:info" + info + b"e"


def _single_file_torrent_bytes() -> bytes:
    name = _encode_str("movie.mkv")
    info = b"d4:name" + name + b"6:lengthi42ee"
    announce = _encode_str("http://tr.libria.fun:2710/announce")
    return b"d8:announce" + announce + b"4:info" + info + b"e"


def test_parse_torrent_file_list_multi() -> None:
    files = parse_torrent_file_list(_multi_file_torrent_bytes())
    assert len(files) == 2
    assert files[0].relative_path == str(Path("Show Name") / "ep01.mkv")
    assert files[0].size == 100
    assert files[0].file_index == 0
    assert files[1].relative_path == str(Path("Show Name") / "ep02.mkv")
    assert files[1].size == 200


def test_parse_torrent_file_list_single() -> None:
    files = parse_torrent_file_list(_single_file_torrent_bytes())
    assert len(files) == 1
    assert files[0].relative_path == "movie.mkv"
    assert files[0].size == 42


def test_hash_gate_skips_reread_when_size_mtime_match(tmp_path: Path) -> None:
    target = tmp_path / "clip.mkv"
    target.write_bytes(b"hello-blake3")
    db = MagicMock()

    # Первый проход — нет записи в БД
    db.scalar.return_value = None
    open_calls: list[str] = []

    real_open = open

    def tracking_open(path, mode="r", *args, **kwargs):
        open_calls.append(str(path))
        return real_open(path, mode, *args, **kwargs)

    first = upsert_disk_hash(db, target, open_fn=tracking_open)
    assert first.hashed is True
    assert first.skipped_gate is False
    assert first.content_hash
    assert len(open_calls) == 1

    # Второй проход — тот же size+mtime
    existing = SimpleNamespace(
        full_path=str(target.resolve()),
        size=first.size,
        mtime=first.mtime,
        content_hash=first.content_hash,
        last_checked_at=None,
    )
    db.scalar.return_value = existing
    open_calls.clear()
    second = upsert_disk_hash(db, target, open_fn=tracking_open)
    assert second.skipped_gate is True
    assert second.hashed is False
    assert second.content_hash == first.content_hash
    assert open_calls == []


def test_update_api_present_for_release() -> None:
    db = MagicMock()
    a = SimpleNamespace(torrent_id=1, api_present=True, superseded=False)
    b = SimpleNamespace(torrent_id=2, api_present=True, superseded=False)
    c = SimpleNamespace(torrent_id=3, api_present=False, superseded=False)
    old = SimpleNamespace(torrent_id=1, api_present=True, superseded=True)
    db.scalars.return_value.all.return_value = [a, b, c, old]

    stats = update_api_present_for_release(db, release_id=10, present_torrent_ids={1, 3})

    assert a.api_present is True
    assert b.api_present is False
    assert c.api_present is True
    assert old.api_present is False
    assert stats["false"] == 2
    assert stats["true"] == 1
    db.commit.assert_called()


def test_mark_missing_api_present_false_skips_empty_seen_set() -> None:
    db = MagicMock()

    archived = mark_missing_api_present_false(db, set())

    assert archived == 0
    db.execute.assert_not_called()
    db.commit.assert_not_called()


def test_process_completion_enqueues_hash_without_blocking_slave(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_MASTER_ADDED,
        error=None,
    )
    claimed = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_MASTER_COMPLETE,
        error=None,
    )
    service._claim_master_complete = MagicMock(return_value=claimed)  # type: ignore[method-assign]

    done = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_SLAVE_ADDED,
        error=None,
    )
    add_called = {"ok": False}

    def fake_add(p, _bytes):
        add_called["ok"] = True
        p.status = TorrentPipelineService.STATUS_SLAVE_ADDED
        return done

    service._add_to_slave = MagicMock(side_effect=fake_add)  # type: ignore[method-assign]
    enqueue = MagicMock()
    service._enqueue_hash_torrent = enqueue  # type: ignore[method-assign]
    service.classify_slave_torrent = MagicMock(return_value="in_progress")  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent")

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    assert add_called["ok"] is True
    enqueue.assert_called_once_with(done)
    # slave add и enqueue независимы: оба вызваны
    service._add_to_slave.assert_called_once()


def test_process_completion_retry_enqueues_hash_if_needed() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_MASTER_COMPLETE,
        error=None,
    )
    done = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_SLAVE_ADDED,
        error=None,
    )
    service._add_to_slave = MagicMock(return_value=done)  # type: ignore[method-assign]
    enqueue = MagicMock()
    service._enqueue_hash_torrent = enqueue  # type: ignore[method-assign]
    service.classify_slave_torrent = MagicMock(return_value="in_progress")  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent")

    assert result.status == TorrentPipelineService.STATUS_SLAVE_ADDED
    service._add_to_slave.assert_called_once()
    enqueue.assert_called_once_with(done)


def test_enqueue_hash_skips_when_already_success() -> None:
    """Skip уже поставленного/завершённого hash — без PipelineEvent (без spam timeline)."""
    db = MagicMock()
    service = TorrentPipelineService(db, job_id=42)
    pipeline = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_DONE,
    )
    db.scalar.return_value = SimpleNamespace(api_present=True)
    service._hash_torrent_already_done_or_queued = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._add_log = MagicMock()  # type: ignore[method-assign]

    service._enqueue_hash_torrent(pipeline)

    service._hash_torrent_already_done_or_queued.assert_called_once_with("abc123")
    service._add_log.assert_called_once()
    assert "пропуск" in service._add_log.call_args.args[0]
    events = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], PipelineEvent)]
    assert events == []


def test_hash_torrent_already_done_allows_retry_after_failed() -> None:
    """failed не попадает в select pending/running/success → можно enqueue снова."""
    db = MagicMock()
    service = TorrentPipelineService(db)
    db.scalars.return_value.all.return_value = []
    assert service._hash_torrent_already_done_or_queued("abc123") is False

    success_job = SimpleNamespace(params_json={"info_hash": "abc123"})
    db.scalars.return_value.all.return_value = [success_job]
    assert service._hash_torrent_already_done_or_queued("abc123") is True


def test_enqueue_hash_marks_failed_when_schedule_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если schedule_job упал после create_job — pending не блокирует retry."""
    from app.services.job_runner import STATUS_FAILED, STATUS_PENDING

    db = MagicMock()
    service = TorrentPipelineService(db)
    pipeline = SimpleNamespace(
        id=1,
        info_hash="abc123",
        release_id=10,
        torrent_id=20,
        status=TorrentPipelineService.STATUS_DONE,
    )
    db.scalar.return_value = SimpleNamespace(api_present=True)
    service._add_log = MagicMock()  # type: ignore[method-assign]

    job = SimpleNamespace(id=99, status=STATUS_PENDING, error=None, finished_at=None)
    fake_runner = MagicMock()
    fake_runner.create_job.return_value = job
    fake_runner.schedule_job.side_effect = RuntimeError("no event loop")
    monkeypatch.setattr("app.api.rest.job_runner", fake_runner)
    db.get.return_value = job
    # Для _hash_torrent_already_done_or_queued после mark: failed не в выборке.
    db.scalars.return_value.all.return_value = []

    assert service._hash_torrent_already_done_or_queued("abc123") is False
    service._enqueue_hash_torrent(pipeline)

    assert job.status == STATUS_FAILED
    assert job.error
    assert job.finished_at is not None
    db.commit.assert_called()
    events = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], PipelineEvent)]
    assert any("не удалось поставить" in (e.message or "") for e in events)
    # После пометки failed повторный enqueue не блокируется.
    assert service._hash_torrent_already_done_or_queued("abc123") is False


def test_mark_hash_job_schedule_failed_retries_after_commit_error() -> None:
    from app.services.job_runner import STATUS_FAILED, STATUS_PENDING

    db = MagicMock()
    service = TorrentPipelineService(db)
    service._add_log = MagicMock()  # type: ignore[method-assign]

    job = SimpleNamespace(id=7, status=STATUS_PENDING, error=None, finished_at=None)
    db.get.return_value = job
    db.commit.side_effect = [RuntimeError("db down"), None]

    service._mark_hash_job_schedule_failed(7, RuntimeError("no loop"))

    assert job.status == STATUS_FAILED
    assert db.rollback.called
    assert db.commit.call_count == 2


def test_format_file_size_mb_and_gb() -> None:
    from app.services.file_hasher import format_file_size

    assert format_file_size(0) == "0.00 MB"
    assert format_file_size(1024 * 1024) == "1.00 MB"
    assert format_file_size(1554591571) == "1.45 GB"  # >= 1024 MB
    assert format_file_size(1024 * 1024 * 1024 - 1).endswith(" MB")
    assert format_file_size(1024 * 1024 * 1024) == "1.00 GB"

def test_hash_torrent_soft_skip_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.jobs import hash_torrent as mod

    db = MagicMock()
    tracker = MagicMock()
    tracker.track_torrent.return_value = SimpleNamespace(
        skipped_reason="торрент не api_present (архивный)",
        files_upserted=0,
        hashed=0,
        gated=0,
        errors=0,
        changes=[],
    )
    monkeypatch.setattr(mod, "FileTrackerService", MagicMock(return_value=tracker))
    asyncio.run(
        mod.run_hash_torrent(
            db,
            1,
            {"info_hash": "a" * 40, "torrent_id": 1, "release_id": 2},
        )
    )


def test_hash_torrent_retriable_skip_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.jobs import hash_torrent as mod

    db = MagicMock()
    tracker = MagicMock()
    tracker.track_torrent.return_value = SimpleNamespace(
        skipped_reason="нет .torrent в архиве",
        files_upserted=0,
        hashed=0,
        gated=0,
        errors=0,
        changes=[],
    )
    monkeypatch.setattr(mod, "FileTrackerService", MagicMock(return_value=tracker))
    with pytest.raises(RuntimeError, match="нет \\.torrent"):
        asyncio.run(
            mod.run_hash_torrent(
                db,
                1,
                {"info_hash": "a" * 40, "torrent_id": 1, "release_id": 2},
            )
        )


def test_sync_composition_persists_added_before_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """added/removed пишутся до хеша — stop не должен их терять."""
    from app.services.file_tracker import KIND_ADDED, _CompositionSync

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "ab" * 20
    torrent_id = 42
    release_id = 7

    db = MagicMock()
    # previous torrent_files empty; prior candidates empty
    db.scalars.return_value.all.return_value = []

    service = FileTrackerService(db)
    persisted: list[list[FileChange]] = []

    def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
        persisted.append(list(changes))
        return [SimpleNamespace(kind=c.kind, relative_path=c.relative_path) for c in changes]

    monkeypatch.setattr(service, "_persist_events", fake_persist)
    monkeypatch.setattr(
        service,
        "_qb_paths_and_priorities",
        lambda _h: (str(media), str(media), {}),
    )
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_media_root",
        lambda: media,
    )
    # Пути не существуют на диске → baseline provisional «новый».
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda *a, **k: media / "Show Name" / "ep01.mkv",
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=torrent_id,
        release_id=release_id,
        torrent_bytes=_multi_file_torrent_bytes(),
    )
    assert isinstance(synced, _CompositionSync)
    assert synced.has_prior_version is False
    assert len(persisted) == 1
    assert all(c.kind == KIND_ADDED for c in persisted[0])
    assert len(persisted[0]) == 2
    assert synced.result.files_upserted == 2


def test_first_seen_normalizes_path_separators() -> None:
    from app.services.file_tracker import FileTrackerService

    assert (
        FileTrackerService.first_seen_for_path(
            has_prior_version=True,
            prior_version_paths={"Show Name/ep01.mkv"},
            relative_path=r"Show Name\ep01.mkv",
        )
        is False
    )
    assert (
        FileTrackerService.first_seen_for_path(
            has_prior_version=True,
            prior_version_paths={"Show Name/ep01.mkv"},
            relative_path="Show Name/ep02.mkv",
        )
        is True
    )


def test_first_seen_empty_prior_composition_keeps_new() -> None:
    """has_prior + пустой состав → first_seen=True (не лечим в ok)."""
    from app.services.file_tracker import FileTrackerService

    assert (
        FileTrackerService.first_seen_for_path(
            has_prior_version=True,
            prior_version_paths=set(),
            relative_path="Show/ep01.mkv",
        )
        is True
    )
    assert (
        FileTrackerService.first_seen_for_path(
            has_prior_version=False,
            prior_version_paths=set(),
            relative_path="Show/ep01.mkv",
        )
        is False
    )


def test_sync_composition_baseline_complete_on_disk_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Первый торрент: complete на диске всё равно new (добавление этой версии)."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "ae" * 20
    ep01 = "Show Name/ep01.mkv"
    ep02 = "Show Name/ep02.mkv"
    full01 = media / ep01
    full01.parent.mkdir(parents=True)
    full01.write_bytes(b"exists")

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)

    def fake_resolve(base, rel, **_k):  # noqa: ANN001
        return media / rel

    monkeypatch.setattr("app.services.file_tracker.resolve_full_path", fake_resolve)
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows[ep01].ui_status == UI_STATUS_NEW
    assert rows[ep02].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep01, ep02}
    assert synced.baseline_had_known is False


def test_sync_composition_early_ok_healed_to_new_on_clean_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early sync ошибочно поставил ok → hash sync чистого baseline чинит в new."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "he" * 20
    paths = []
    rows = []
    for idx in range(1, 5):
        rel = f"Show/ep0{idx}.mkv"
        full = media / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"x")
        paths.append(rel)
        rows.append(
            SimpleNamespace(
                relative_path=rel,
                torrent_id=1,
                release_id=2,
                size=idx,
                file_index=idx - 1,
                selected=True,
                full_path=str(full),
                ui_status=UI_STATUS_OK if idx == 4 else UI_STATUS_NEW,
            )
        )

    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    db.scalar.return_value = None

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel, size=i + 1, file_index=i)
            for i, rel in enumerate(paths)
        ],
    )
    monkeypatch.setattr(service, "_load_hashed_canonical_paths", lambda _p: set())

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.baseline_had_known is False
    assert synced.first_seen_paths == set(paths)
    assert all(r.ui_status == UI_STATUS_NEW for r in rows)


def test_sync_composition_mixed_baseline_complete_without_hash_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prior=no, в составе уже ok/ok, новый эпизод complete без хеша → new."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "b2" * 20
    ep01 = "Show/ep01.mkv"
    ep02 = "Show/ep02.mkv"
    ep03 = "Show/ep03.mkv"
    for rel in (ep01, ep02, ep03):
        path = media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    row01 = SimpleNamespace(
        relative_path=ep01,
        torrent_id=39150,
        release_id=10227,
        size=1,
        file_index=0,
        selected=True,
        full_path=str(media / ep01),
        ui_status=UI_STATUS_OK,
    )
    row02 = SimpleNamespace(
        relative_path=ep02,
        torrent_id=39150,
        release_id=10227,
        size=2,
        file_index=1,
        selected=True,
        full_path=str(media / ep02),
        ui_status=UI_STATUS_OK,
    )

    db = MagicMock()
    db.scalars.return_value.all.return_value = [row01, row02]
    db.scalar.return_value = None
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
            SimpleNamespace(relative_path=ep03, size=3, file_index=2),
        ],
    )
    monkeypatch.setattr(
        service,
        "_load_hashed_canonical_paths",
        lambda paths: {str(media / ep01), str(media / ep02)},
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=39150,
        release_id=10227,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    assert synced.baseline_had_known is True
    assert row01.ui_status == UI_STATUS_OK
    assert row02.ui_status == UI_STATUS_OK
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows[ep03].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep03}


def test_sync_composition_partial_backfill_without_known_is_all_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой состав + partial hashed_paths → всё new (первый торрент = добавления)."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "b3" * 20
    ep01 = "Show/ep01.mkv"
    ep02 = "Show/ep02.mkv"
    ep03 = "Show/ep03.mkv"
    for rel in (ep01, ep02, ep03):
        path = media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
            SimpleNamespace(relative_path=ep03, size=3, file_index=2),
        ],
    )
    monkeypatch.setattr(
        service,
        "_load_hashed_canonical_paths",
        lambda paths: {str(media / ep01), str(media / ep02)},
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.baseline_had_known is False
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows[ep01].ui_status == UI_STATUS_NEW
    assert rows[ep02].ui_status == UI_STATUS_NEW
    assert rows[ep03].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep01, ep02, ep03}


def test_sync_composition_prior_keeps_existing_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Есть prior: файл из прошлой версии → ok; нового эпизода → new (не все new)."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "af" * 20
    ep01 = "Show Name/ep01.mkv"
    ep02 = "Show Name/ep02.mkv"

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: "prev")
    monkeypatch.setattr(service, "_prior_version_files", lambda **_k: {ep01: None})
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is True
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows[ep01].ui_status == UI_STATUS_OK
    assert rows[ep02].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep02}


def test_sync_heals_false_sticky_new_when_prior_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory/early-sync без prior успел поставить всем new → sync с prior лечит 1–3 в ok.

    Регресс «Uchi…»: все бейджи «новый», хотя relative_path совпали с prior той же torrent_id.
    """
    from app.services.file_tracker import (
        TorrentFile,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "u2" * 20
    prior_hash = "u1" * 20
    ep01 = "Uchi/ep_[01].mkv"
    ep02 = "Uchi/ep_[02].mkv"
    ep03 = "Uchi/ep_[03].mkv"
    ep04 = "Uchi/ep_[04].mkv"
    prior_paths = {ep01, ep02, ep03}

    # Строки уже созданы inventory как baseline-new (prior ещё не был виден).
    existing = {
        rel: TorrentFile(
            torrent_id=77,
            info_hash=info_hash,
            release_id=9,
            relative_path=rel,
            size=1,
            file_index=idx,
            selected=True,
            full_path=str(media / rel),
            ui_status=UI_STATUS_NEW,
        )
        for idx, rel in enumerate((ep01, ep02, ep03, ep04))
    }

    db = MagicMock()
    db.scalars.return_value.all.return_value = list(existing.values())
    db.scalar.return_value = SimpleNamespace(id=42)  # prior archive id
    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(
        service, "_prior_version_files", lambda **_k: {p: None for p in prior_paths}
    )
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _b, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel, size=1, file_index=i)
            for i, rel in enumerate((ep01, ep02, ep03, ep04))
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=77,
        release_id=9,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is True
    assert synced.prior_info_hash == prior_hash
    assert synced.prior_archive_id == 42
    assert existing[ep01].ui_status == UI_STATUS_OK
    assert existing[ep02].ui_status == UI_STATUS_OK
    assert existing[ep03].ui_status == UI_STATUS_OK
    assert existing[ep04].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep04}
    heals = [
        t
        for t in synced.ui_transitions
        if t.from_status == UI_STATUS_NEW and t.to_status == UI_STATUS_OK
    ]
    assert {t.relative_path for t in heals} == {ep01, ep02, ep03}

    FileTrackerService._settle_ui_status(
        existing[ep01], first_seen=False, mismatch=False, matched=True
    )
    FileTrackerService._settle_ui_status(
        existing[ep04], first_seen=True, mismatch=False, matched=False
    )
    assert existing[ep01].ui_status == UI_STATUS_OK
    assert existing[ep04].ui_status == UI_STATUS_NEW


def test_sync_emits_create_ui_status_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case B: ранний sync пишет ui_status с (create)→new даже без prior (Bleach)."""
    from app.services.file_tracker import UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "b1" * 20
    ep01 = "Bleach/ep01.mkv"
    ep02 = "Bleach/ep02.mkv"

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    created: list = []
    db.add.side_effect = lambda obj: created.append(obj)
    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _b, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=39186,
        release_id=8452,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    creates = [
        t
        for t in synced.ui_transitions
        if t.from_status in {"(create)", "create"} and t.to_status == UI_STATUS_NEW
    ]
    assert {t.relative_path for t in creates} == {ep01, ep02}


def test_uchi_race_early_sync_then_prior_then_hash_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регресс Case A: early sync без prior → все new; sync с prior (все 4 пути) → ok;
    hash settle: 1–3 matched → ok, ep04 mismatch → changed.
    """
    from app.services.file_tracker import (
        TorrentFile,
        TrackTorrentResult,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    (media / "Uchi").mkdir(parents=True)
    torrent_id = 39184
    release_id = 10272
    info_hash = "c2" * 20
    prior_hash = "c1" * 20
    ep01 = "Uchi/ep_[01].mkv"
    ep02 = "Uchi/ep_[02].mkv"
    ep03 = "Uchi/ep_[03].mkv"
    ep04 = "Uchi/ep_[04].mkv"
    paths = (ep01, ep02, ep03, ep04)

    # 1) Early sync: prior ещё не виден → все new
    existing = {
        rel: TorrentFile(
            torrent_id=torrent_id,
            info_hash=info_hash,
            release_id=release_id,
            relative_path=rel,
            size=1,
            file_index=idx,
            selected=True,
            full_path=str((media / rel).resolve()),
            ui_status=UI_STATUS_NEW,
        )
        for idx, rel in enumerate(paths)
    }
    for rel in paths:
        p = media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"body:" + rel.encode())

    db = MagicMock()
    db.scalars.return_value.all.return_value = list(existing.values())
    db.scalar.return_value = SimpleNamespace(id=99)
    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(
        service,
        "_prior_version_files",
        lambda **_k: {
            ep01: str(media / ep01),
            ep02: str(media / ep02),
            ep03: str(media / ep03),
            ep04: str(media / ep04),
        },
    )
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _b, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel, size=1, file_index=i)
            for i, rel in enumerate(paths)
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=torrent_id,
        release_id=release_id,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is True
    assert existing[ep01].ui_status == UI_STATUS_OK
    assert existing[ep02].ui_status == UI_STATUS_OK
    assert existing[ep03].ui_status == UI_STATUS_OK
    assert existing[ep04].ui_status == UI_STATUS_OK  # был в prior — provisional ok
    assert synced.first_seen_paths == set()
    heals = [
        t
        for t in synced.ui_transitions
        if t.from_status == UI_STATUS_NEW and t.to_status == UI_STATUS_OK
    ]
    assert {t.relative_path for t in heals} == {ep01, ep02, ep03, ep04}

    # 2) Hash settle как в track_torrent
    prior_hashes = {ep01: "h01", ep02: "h02", ep03: "h03", ep04: "h04-old"}
    new_hashes = {
        existing[ep01].full_path: "h01",
        existing[ep02].full_path: "h02",
        existing[ep03].full_path: "h03",
        existing[ep04].full_path: "h04-new",
    }
    db2 = MagicMock()
    db2.scalars.return_value.all.return_value = list(existing.values())

    def scalar_for_path(stmt=None, **_k):  # noqa: ANN001
        try:
            params = list((stmt.compile().params or {}).values())
        except Exception:
            params = []
        for val in params:
            if isinstance(val, str) and val in new_hashes:
                return SimpleNamespace(content_hash=new_hashes[val], full_path=val)
        return None

    db2.scalar.side_effect = scalar_for_path
    tracker = FileTrackerService(db2)
    monkeypatch.setattr(
        tracker,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(tracker, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(tracker, "_prior_version_hashes", lambda **_k: dict(prior_hashes))
    monkeypatch.setattr(tracker, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(tracker, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(tracker, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(tracker, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(
        tracker,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(
                has_prior_version=True,
                prior_info_hash=prior_hash,
                prior_archive_id=99,
            ),
            events=[],
            has_prior_version=True,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths=set(),
            baseline_had_known=False,
            prior_info_hash=prior_hash,
            prior_archive_id=99,
            ui_transitions=[],
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 4, "gated": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "app.services.file_tracker.path_exists_including_incomplete", lambda _p: True
    )
    monkeypatch.setattr("app.services.file_tracker.is_under_media_root", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None
    )

    result = tracker.track_torrent(
        info_hash=info_hash, torrent_id=torrent_id, release_id=release_id, notify=False
    )
    assert result.has_prior_version is True
    assert existing[ep01].ui_status == UI_STATUS_OK
    assert existing[ep02].ui_status == UI_STATUS_OK
    assert existing[ep03].ui_status == UI_STATUS_OK
    assert existing[ep04].ui_status == UI_STATUS_CHANGED
    assert all(existing[r].ui_status != UI_STATUS_NEW for r in (ep01, ep02, ep03, ep04))


def test_prior_fallback_same_release_different_torrent_id() -> None:
    """AniLibria новый torrent_id: prior по release_id + exact relative_path overlap."""
    db = MagicMock()
    current = "aa" * 20
    prior_h = "bb" * 20
    other_h = "cc" * 20  # другой рип, путей overlap
    current_row = SimpleNamespace(
        id=12,
        info_hash=current,
        torrent_id=39184,
        release_id=10272,
        torrent_type="BDRip 1080p AVC",
        quality_json={},
        superseded=False,
        api_present=True,
    )
    prior_row = SimpleNamespace(
        id=10,
        info_hash=prior_h,
        torrent_id=100,
        release_id=10272,
        torrent_type="BDRip 1080p AVC",
        quality_json={},
        superseded=True,
        api_present=False,
    )
    other_row = SimpleNamespace(
        id=11,
        info_hash=other_h,
        torrent_id=101,
        release_id=10272,
        torrent_type="WEBRip 1080p AVC",
        quality_json={},
        superseded=False,
        api_present=True,
    )
    current_paths = {
        "Uchi/ep_[01].mkv",
        "Uchi/ep_[02].mkv",
        "Uchi/ep_[03].mkv",
        "Uchi/ep_[04].mkv",
    }
    prior_files = [
        SimpleNamespace(info_hash=prior_h, relative_path="Uchi/ep_[01].mkv", full_path="/m/1"),
        SimpleNamespace(info_hash=prior_h, relative_path="Uchi/ep_[02].mkv", full_path="/m/2"),
        SimpleNamespace(info_hash=prior_h, relative_path="Uchi/ep_[03].mkv", full_path="/m/3"),
    ]
    other_files = [
        SimpleNamespace(info_hash=other_h, relative_path="Other/ep01.mkv", full_path="/m/x"),
    ]

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    calls = {"n": 0}

    def fake_scalars(_stmt):
        calls["n"] += 1
        # 1) same torrent_id → empty
        if calls["n"] == 1:
            return FakeScalars([])
        # 2) same release archives (в т.ч. current для codec/rip family)
        if calls["n"] == 2:
            return FakeScalars([current_row, other_row, prior_row])
        # 3) torrent_files for candidates
        return FakeScalars(prior_files + other_files)

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    archive = service._prior_version_archive(  # type: ignore[attr-defined]
        torrent_id=39184,
        current_hash=current,
        release_id=10272,
        current_paths=current_paths,
    )
    assert archive is not None
    assert archive.info_hash == prior_h
    assert archive.torrent_id == 100


def test_prior_fallback_ignores_opposite_codec_sibling() -> None:
    """AVC republish не берёт активный HEVC sibling как sticky prior при похожих путях."""
    db = MagicMock()
    current = "aa" * 20
    avc_prior_h = "bb" * 20
    hevc_h = "cc" * 20
    # overlapping episode basenames / paths между AVC и HEVC
    shared_paths = {
        "Show/ep_[01].mkv",
        "Show/ep_[02].mkv",
        "Show/ep_[03].mkv",
    }
    current_row = SimpleNamespace(
        id=30,
        info_hash=current,
        torrent_id=5002,
        release_id=9001,
        torrent_type="BDRip 1080p AVC",
        quality_json={"type": "BDRip", "quality": "1080p", "codec": "AVC"},
        superseded=False,
        api_present=True,
    )
    # HEVC sibling: больше overlap (все 3 + extra), активный — раньше ложно выигрывал max overlap
    hevc_row = SimpleNamespace(
        id=29,
        info_hash=hevc_h,
        torrent_id=5001,
        release_id=9001,
        torrent_type="BDRip 1080p HEVC",
        quality_json={"type": "BDRip", "quality": "1080p", "codec": "HEVC"},
        superseded=False,
        api_present=True,
    )
    avc_prior_row = SimpleNamespace(
        id=20,
        info_hash=avc_prior_h,
        torrent_id=5000,
        release_id=9001,
        torrent_type="BDRip 1080p AVC",
        quality_json={"type": "BDRip", "quality": "1080p", "codec": "AVC"},
        superseded=True,
        api_present=False,
    )
    avc_prior_files = [
        SimpleNamespace(info_hash=avc_prior_h, relative_path=p, full_path=f"/m/avc/{i}")
        for i, p in enumerate(sorted(shared_paths)[:2], start=1)
    ]
    hevc_files = [
        SimpleNamespace(info_hash=hevc_h, relative_path=p, full_path=f"/m/hevc/{i}")
        for i, p in enumerate(sorted(shared_paths), start=1)
    ] + [
        SimpleNamespace(
            info_hash=hevc_h, relative_path="Show/ep_[04].mkv", full_path="/m/hevc/4"
        )
    ]

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    calls = {"n": 0}

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeScalars([])  # same torrent_id
        if calls["n"] == 2:
            # HEVC первым (новее) — старый max-overlap выбрал бы его
            return FakeScalars([current_row, hevc_row, avc_prior_row])
        return FakeScalars(avc_prior_files + hevc_files)

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    archive = service._prior_version_archive(  # type: ignore[attr-defined]
        torrent_id=5002,
        current_hash=current,
        release_id=9001,
        current_paths=shared_paths,
    )
    assert archive is not None
    assert archive.info_hash == avc_prior_h
    assert archive.torrent_id == 5000
    assert "HEVC" not in (archive.torrent_type or "")


def test_prior_fallback_no_same_codec_history_skips_hevc_sibling() -> None:
    """Первый AVC на релизе с активным HEVC: HEVC не prior → нет sticky prior."""
    db = MagicMock()
    current = "aa" * 20
    hevc_h = "cc" * 20
    paths = {"Show/ep01.mkv", "Show/ep02.mkv"}
    current_row = SimpleNamespace(
        id=2,
        info_hash=current,
        torrent_id=7002,
        release_id=8001,
        torrent_type="BDRip 1080p AVC",
        quality_json={},
        superseded=False,
        api_present=True,
    )
    hevc_row = SimpleNamespace(
        id=1,
        info_hash=hevc_h,
        torrent_id=7001,
        release_id=8001,
        torrent_type="BDRip 1080p HEVC",
        quality_json={},
        superseded=False,
        api_present=True,
    )
    hevc_files = [
        SimpleNamespace(info_hash=hevc_h, relative_path="Show/ep01.mkv", full_path="/m/1"),
        SimpleNamespace(info_hash=hevc_h, relative_path="Show/ep02.mkv", full_path="/m/2"),
    ]

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    calls = {"n": 0}

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeScalars([])
        if calls["n"] == 2:
            return FakeScalars([current_row, hevc_row])
        return FakeScalars(hevc_files)

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    archive = service._prior_version_archive(  # type: ignore[attr-defined]
        torrent_id=7002,
        current_hash=current,
        release_id=8001,
        current_paths=paths,
    )
    assert archive is None


def test_baseline_disk_mismatch_stays_new_not_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case A без prior: disk rehash ≠ changed; все остаются new (sticky rules)."""
    from app.services.file_tracker import (
        TrackTorrentResult,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
    )

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "d2" * 20
    rel = "Show/ep04.mkv"
    full = media / rel
    full.parent.mkdir(parents=True)
    full.write_bytes(b"new-body")
    row = SimpleNamespace(
        relative_path=rel,
        selected=True,
        full_path=str(full.resolve()),
        ui_status=UI_STATUS_NEW,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [row]
    db.scalar.return_value = SimpleNamespace(content_hash="new-hash", full_path=str(full.resolve()))
    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rel},
            baseline_had_known=False,
            prior_info_hash=None,
            prior_archive_id=None,
            ui_transitions=[],
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 1, "gated": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "app.services.file_tracker.path_exists_including_incomplete", lambda _p: True
    )
    monkeypatch.setattr("app.services.file_tracker.is_under_media_root", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None
    )

    result = service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=False)
    assert result.has_prior_version is False
    assert row.ui_status == UI_STATUS_NEW
    assert row.ui_status != UI_STATUS_CHANGED


def test_sync_empty_prior_composition_does_not_heal_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """has_prior=True, но состав prior пуст/неизвестен → sticky new не лечим в ok."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "e2" * 20
    prior_hash = "e1" * 20
    ep01 = "EmptyPrior/ep01.mkv"
    ep02 = "EmptyPrior/ep02.mkv"

    existing = {
        rel: TorrentFile(
            torrent_id=88,
            info_hash=info_hash,
            release_id=3,
            relative_path=rel,
            size=1,
            file_index=idx,
            selected=True,
            full_path=str(media / rel),
            ui_status=UI_STATUS_NEW,
        )
        for idx, rel in enumerate((ep01, ep02))
    }

    db = MagicMock()
    db.scalars.return_value.all.return_value = list(existing.values())
    db.scalar.return_value = SimpleNamespace(id=7)
    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(service, "_prior_version_files", lambda **_k: {})
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _b, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel, size=1, file_index=i)
            for i, rel in enumerate((ep01, ep02))
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=88,
        release_id=3,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is True
    assert synced.first_seen_paths == {ep01, ep02}
    assert existing[ep01].ui_status == UI_STATUS_NEW
    assert existing[ep02].ui_status == UI_STATUS_NEW
    heals = [
        t
        for t in synced.ui_transitions
        if t.from_status == UI_STATUS_NEW and t.to_status == UI_STATUS_OK
    ]
    assert heals == []

    # settle тоже не лечит без path_in_prior
    FileTrackerService._settle_ui_status(
        existing[ep01],
        first_seen=True,
        mismatch=False,
        matched=False,
        path_in_prior=False,
    )
    assert existing[ep01].ui_status == UI_STATUS_NEW
    FileTrackerService._settle_ui_status(
        existing[ep02],
        first_seen=False,
        mismatch=False,
        matched=False,
        path_in_prior=False,
    )
    assert existing[ep02].ui_status == UI_STATUS_NEW


def test_uchi_add_ep04_then_rebuild_sticky_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1 eps1–3 → all new; v2 +ep04 → 1–3 ok / 04 new; v3 rebuild 04 → changed."""
    from app.services.file_tracker import (
        FileTrackerService,
        TorrentFile,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    (media / "Uchi").mkdir(parents=True)
    torrent_id = 77
    release_id = 9
    v1, v2, v3 = "a1" * 20, "a2" * 20, "a3" * 20
    ep01, ep02, ep03, ep04 = (
        "Uchi/ep_[01].mkv",
        "Uchi/ep_[02].mkv",
        "Uchi/ep_[03].mkv",
        "Uchi/ep_[04].mkv",
    )

    def touch(rel: str, body: bytes) -> str:
        path = media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return str(path.resolve())

    rows_by_hash: dict[str, dict[str, object]] = {}

    def sync(*, info_hash: str, prior_hash: str | None, prior_files: dict[str, str], metas: list):
        db = MagicMock()
        db.scalars.return_value.all.return_value = list(
            rows_by_hash.get(info_hash, {}).values()
        )
        db.scalar.return_value = SimpleNamespace(id=10) if prior_hash else None

        def add(obj):
            if isinstance(obj, TorrentFile):
                rows_by_hash.setdefault(info_hash, {})[obj.relative_path] = obj

        db.add.side_effect = add
        service = FileTrackerService(db)
        monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
        monkeypatch.setattr(service, "_prior_version_files", lambda **_k: dict(prior_files))
        monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
        monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
        monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
        monkeypatch.setattr(
            service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {})
        )
        monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
        monkeypatch.setattr(
            "app.services.file_tracker.resolve_full_path",
            lambda _b, rel, **_k: media / rel,
        )
        monkeypatch.setattr(
            "app.services.file_tracker.parse_torrent_file_list", lambda _b: metas
        )
        return service._sync_composition(  # type: ignore[attr-defined]
            normalized_hash=info_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=b"x",
        )

    full1 = {
        ep01: touch(ep01, b"1"),
        ep02: touch(ep02, b"2"),
        ep03: touch(ep03, b"3"),
    }
    metas1 = [
        SimpleNamespace(relative_path=r, size=1, file_index=i)
        for i, r in enumerate((ep01, ep02, ep03))
    ]
    s1 = sync(info_hash=v1, prior_hash=None, prior_files={}, metas=metas1)
    assert s1.has_prior_version is False
    assert {r: rows_by_hash[v1][r].ui_status for r in rows_by_hash[v1]} == {
        ep01: UI_STATUS_NEW,
        ep02: UI_STATUS_NEW,
        ep03: UI_STATUS_NEW,
    }

    full2 = {**full1, ep04: touch(ep04, b"4")}
    metas2 = [
        SimpleNamespace(relative_path=r, size=1, file_index=i)
        for i, r in enumerate((ep01, ep02, ep03, ep04))
    ]
    s2 = sync(info_hash=v2, prior_hash=v1, prior_files=full1, metas=metas2)
    assert s2.has_prior_version is True
    rows2 = rows_by_hash[v2]
    assert rows2[ep01].ui_status == UI_STATUS_OK
    assert rows2[ep02].ui_status == UI_STATUS_OK
    assert rows2[ep03].ui_status == UI_STATUS_OK
    assert rows2[ep04].ui_status == UI_STATUS_NEW
    assert s2.first_seen_paths == {ep04}

    for rel in (ep01, ep02, ep03):
        FileTrackerService._settle_ui_status(
            rows2[rel], first_seen=False, mismatch=False, matched=True
        )
    FileTrackerService._settle_ui_status(
        rows2[ep04], first_seen=True, mismatch=False, matched=False
    )
    assert rows2[ep01].ui_status == UI_STATUS_OK
    assert rows2[ep04].ui_status == UI_STATUS_NEW

    sync(info_hash=v3, prior_hash=v2, prior_files=full2, metas=metas2)
    rows3 = rows_by_hash[v3]
    assert rows3[ep01].ui_status == UI_STATUS_OK
    assert rows3[ep04].ui_status == UI_STATUS_OK  # provisional до hash
    FileTrackerService._settle_ui_status(
        rows3[ep01], first_seen=False, mismatch=False, matched=True
    )
    FileTrackerService._settle_ui_status(
        rows3[ep04], first_seen=False, mismatch=True, matched=False
    )
    assert rows3[ep01].ui_status == UI_STATUS_OK
    assert rows3[ep04].ui_status == UI_STATUS_CHANGED


def test_emit_ui_status_pipeline_event_writes_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переходы ui_status пишутся в pipeline_events с prior/from→to."""
    from app.services.file_tracker import UiStatusTransition

    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(id=15, status="done")
    service = FileTrackerService(db, job_id=99)
    recorded: list = []

    def fake_record(db, pipeline_id, **kwargs):  # noqa: ANN001
        recorded.append((pipeline_id, kwargs))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(
        "app.services.pipeline.record_pipeline_event", fake_record
    )
    service._emit_ui_status_pipeline_event(  # type: ignore[attr-defined]
        info_hash="ab" * 20,
        torrent_id=77,
        phase="sync_composition",
        transitions=[
            UiStatusTransition(
                relative_path="Uchi/ep_[01].mkv",
                from_status="new",
                to_status="ok",
                phase="sync_composition",
                reason="heal_prior_path",
                content_hash_short=None,
            )
        ],
        has_prior_version=True,
        prior_info_hash="cd" * 20,
        prior_archive_id=42,
    )
    assert len(recorded) == 1
    pid, kwargs = recorded[0]
    assert pid == 15
    assert kwargs["event_type"] == "ui_status"
    assert kwargs["job_id"] == 99
    details = kwargs["details"]
    assert details["has_prior"] is True
    assert details["prior_info_hash"] == "cd" * 20
    assert details["prior_archive_id"] == 42
    assert details["transitions"][0]["from"] == "new"
    assert details["transitions"][0]["to"] == "ok"


def test_emit_ui_status_prefers_non_failed_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """_emit_ui_status выбирает pipeline как get_latest_by_hash (не голый id DESC)."""
    from app.services.file_tracker import UiStatusTransition
    from app.services.pipeline import TorrentPipelineService

    db = MagicMock()
    service = FileTrackerService(db, job_id=1)
    recorded: list = []

    def fake_record(db, pipeline_id, **kwargs):  # noqa: ANN001
        recorded.append(pipeline_id)
        return SimpleNamespace(id=1)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    monkeypatch.setattr(
        TorrentPipelineService,
        "get_latest_by_hash",
        lambda self, h: SimpleNamespace(id=99, status="hashing"),
    )
    service._emit_ui_status_pipeline_event(  # type: ignore[attr-defined]
        info_hash="ab" * 20,
        torrent_id=1,
        phase="hash_settle",
        transitions=[
            UiStatusTransition(
                relative_path="a.mkv",
                from_status="new",
                to_status="ok",
                phase="hash_settle",
                reason="hash_match",
            )
        ],
        has_prior_version=True,
        prior_info_hash="cd" * 20,
        prior_archive_id=None,
    )
    assert recorded == [99]


def test_emit_ui_status_logs_warning_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибки audit trail логируются warning, не глотаются молча."""
    from app.services.file_tracker import UiStatusTransition

    db = MagicMock()
    service = FileTrackerService(db)
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_log", lambda msg, level="info": logs.append((msg, level)))
    monkeypatch.setattr(
        "app.services.pipeline.TorrentPipelineService.get_latest_by_hash",
        lambda self, h: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    service._emit_ui_status_pipeline_event(  # type: ignore[attr-defined]
        info_hash="ab" * 20,
        torrent_id=1,
        phase="sync_composition",
        transitions=[
            UiStatusTransition(
                relative_path="a.mkv",
                from_status="new",
                to_status="ok",
                phase="sync_composition",
                reason="heal_prior_path",
            )
        ],
        has_prior_version=False,
        prior_info_hash=None,
        prior_archive_id=None,
    )
    assert any("ui_status pipeline event" in m and lvl == "warning" for m, lvl in logs)


def test_sync_composition_lv999_version_bump_by_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новая версия: [06] и [05](1) → new; [01–04] → ok; старый [05] → removed.

    Сравнение строго по relative_path: «[05] (1).mkv» ≠ «[05].mkv».
    """
    from app.services.file_tracker import (
        KIND_ADDED,
        KIND_REMOVED,
        TorrentFile,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    folder = "Lv999 no Murabito - AniLiberty [WEBRip 1080p HEVC]"
    (media / folder).mkdir(parents=True)
    info_hash = "n1" * 20
    prior_hash = "o1" * 20

    def rel(name: str) -> str:
        return f"{folder}/{name}"

    ep01 = rel("Lv999_no_Murabito_[01]_[HEVC].mkv")
    ep02 = rel("Lv999_no_Murabito_[02]_[HEVC].mkv")
    ep03 = rel("Lv999_no_Murabito_[03]_[HEVC].mkv")
    ep04 = rel("Lv999_no_Murabito_[04]_[HEVC].mkv")
    ep05 = rel("Lv999_no_Murabito_[05]_[HEVC].mkv")
    ep05_renamed = rel("Lv999_no_Murabito_[05]_[HEVC] (1).mkv")
    ep06 = rel("Lv999_no_Murabito_[06]_[HEVC].mkv")

    prior_paths = {ep01, ep02, ep03, ep04, ep05}
    prior_files = {p: str(media / p) for p in prior_paths}
    for full in prior_files.values():
        Path(full).parent.mkdir(parents=True, exist_ok=True)
        Path(full).write_bytes(b"old")

    new_metas = [
        SimpleNamespace(relative_path=ep06, size=6, file_index=0),
        SimpleNamespace(relative_path=ep05_renamed, size=5, file_index=1),
        SimpleNamespace(relative_path=ep04, size=4, file_index=2),
        SimpleNamespace(relative_path=ep03, size=3, file_index=3),
        SimpleNamespace(relative_path=ep02, size=2, file_index=4),
        SimpleNamespace(relative_path=ep01, size=1, file_index=5),
    ]
    for meta in new_metas:
        path = media / meta.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * int(meta.size))

    db = MagicMock()
    db.scalars.return_value.all.return_value = []  # previous for new info_hash
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(service, "_prior_version_files", lambda **_k: dict(prior_files))
    persisted: list = []

    def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
        persisted.extend(changes)
        return []

    monkeypatch.setattr(service, "_persist_events", fake_persist)
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: False)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: new_metas,
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=39199,
        release_id=9001,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is True
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert set(rows) == {ep01, ep02, ep03, ep04, ep05_renamed, ep06}
    assert rows[ep01].ui_status == UI_STATUS_OK
    assert rows[ep02].ui_status == UI_STATUS_OK
    assert rows[ep03].ui_status == UI_STATUS_OK
    assert rows[ep04].ui_status == UI_STATUS_OK
    assert rows[ep05_renamed].ui_status == UI_STATUS_NEW
    assert rows[ep06].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep05_renamed, ep06}

    by_kind: dict[str, set[str]] = {}
    for change in persisted:
        by_kind.setdefault(change.kind, set()).add(change.relative_path)
    assert by_kind.get(KIND_ADDED) == {ep05_renamed, ep06}
    assert by_kind.get(KIND_REMOVED) == {ep05}


def _assert_telegram_file_changes(
    *,
    release_id: int,
    torrent_id: int,
    composition_changes: list,
    expected: dict[str, set[str]],
    baseline: bool = False,
    modified_paths: set[str] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет _maybe_notify_telegram: kinds/paths как в UI (ok в TG не шлём)."""
    from app.services.file_tracker import KIND_MODIFIED
    from app.services.telegram_notify import escape_markdown_v2
    import app.services.telegram_notify as tg

    events = [
        SimpleNamespace(
            kind=c.kind,
            relative_path=c.relative_path,
            full_path=getattr(c, "full_path", None),
        )
        for c in composition_changes
    ]
    for rel in sorted(modified_paths or ()):
        events.append(
            SimpleNamespace(kind=KIND_MODIFIED, relative_path=rel, full_path=None)
        )

    captured: list = []

    def fake_enqueue(_db, **kwargs):  # noqa: ANN001
        captured.append(kwargs)
        return None

    monkeypatch.setattr(tg, "enqueue_file_changes_notification", fake_enqueue)

    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        enabled=True, release_alias="show", title="Show"
    )
    FileTrackerService(db)._maybe_notify_telegram(  # type: ignore[attr-defined]
        release_id=release_id,
        torrent_id=torrent_id,
        events=events,
        archive=SimpleNamespace(torrent_type="HEVC", torrent_description="test"),
        baseline=baseline,
    )

    if not expected:
        assert captured == []
        return

    assert len(captured) == 1
    assert captured[0]["baseline"] is baseline
    got: dict[str, set[str]] = {}
    for ev in captured[0]["events"]:
        got.setdefault(ev.kind, set()).add(ev.relative_path)
    assert got == expected

    changes = [
        {"kind": e.kind, "relative_path": e.relative_path}
        for e in captured[0]["events"]
    ]
    text = build_file_changes_notification_text(
        title="Show",
        alias="show",
        torrent_label="HEVC · test",
        changes=changes,
        baseline=baseline,
    )
    icons = {"added": "➕", "removed": "➖", "modified": "✏️"}
    for kind, paths in expected.items():
        assert icons[kind] in text
        for path in paths:
            assert f"`{escape_markdown_v2(path)}`" in text


def test_three_versions_sticky_statuses_removed_only_on_bump_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Три версии: sticky история; removed только у версии, где файл исчез из состава.

    v1: file_1/2/3/file__1 → new
    v2: 1–2 ok, 3 changed, file__1 removed, file_4 new
    v3: 1–4 ok, file_5 new; file__1 больше не removed
    TG: те же изменения (➕/✏️/➖); ok в сообщение не попадают.
    """
    from app.services.file_tracker import (
        KIND_ADDED,
        KIND_MODIFIED,
        KIND_REMOVED,
        FileTrackerService,
        TorrentFile,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    (media / "Show").mkdir(parents=True)
    torrent_id = 42
    release_id = 7
    v1, v2, v3 = "b1" * 20, "b2" * 20, "b3" * 20

    f1, f2, f3 = "Show/file_1.mkv", "Show/file_2.mkv", "Show/file_3.mkv"
    f_dup, f4, f5 = "Show/file__1.mkv", "Show/file_4.mkv", "Show/file_5.mkv"

    def touch(rel: str, body: bytes) -> str:
        path = media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return str(path.resolve())

    full_v1 = {
        f1: touch(f1, b"v1-1"),
        f2: touch(f2, b"v1-2"),
        f3: touch(f3, b"v1-3"),
        f_dup: touch(f_dup, b"v1-dup"),
    }
    full_v2 = {
        f1: touch(f1, b"v1-1"),
        f2: touch(f2, b"v1-2"),
        f3: touch(f3, b"v2-3-rebuilt"),
        f4: touch(f4, b"v2-4"),
    }
    full_v3 = {
        f1: touch(f1, b"v1-1"),
        f2: touch(f2, b"v1-2"),
        f3: touch(f3, b"v2-3-rebuilt"),
        f4: touch(f4, b"v2-4"),
        f5: touch(f5, b"v3-5"),
    }

    rows_by_hash: dict[str, dict[str, object]] = {}
    persisted: list = []
    event_keys: set[tuple[str, str, str]] = set()

    def rows_for(info_hash: str) -> list:
        return list(rows_by_hash.get(info_hash, {}).values())

    def sync(
        *,
        info_hash: str,
        prior_hash: str | None,
        prior_files: dict[str, str],
        metas: list,
    ):
        db = MagicMock()
        db.scalars.return_value.all.return_value = rows_for(info_hash)
        created: list[object] = []

        def add(obj):
            created.append(obj)
            if isinstance(obj, TorrentFile):
                rows_by_hash.setdefault(info_hash, {})[obj.relative_path] = obj

        db.add.side_effect = add

        service = FileTrackerService(db)
        monkeypatch.setattr(
            service, "_prior_version_hash", lambda **_k: prior_hash
        )
        monkeypatch.setattr(
            service, "_prior_version_files", lambda **_k: dict(prior_files)
        )

        def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
            out = []
            for change in changes:
                key = (
                    (info_hash or "").lower(),
                    change.kind,
                    change.relative_path or change.full_path or "",
                )
                event_keys.add(key)
                persisted.append(change)
                out.append(
                    SimpleNamespace(
                        kind=change.kind,
                        relative_path=change.relative_path,
                        full_path=change.full_path,
                        info_hash=info_hash,
                    )
                )
            return out

        def has_prior(*, torrent_id, info_hash=None, kind, relative_path, full_path):  # noqa: ANN001
            key = (
                (info_hash or "").lower(),
                kind,
                relative_path or full_path or "",
            )
            return key in event_keys

        monkeypatch.setattr(service, "_persist_events", fake_persist)
        monkeypatch.setattr(service, "_has_prior_event", has_prior)
        monkeypatch.setattr(
            service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {})
        )
        monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
        monkeypatch.setattr(
            "app.services.file_tracker.resolve_full_path",
            lambda _base, rel, **_k: media / rel,
        )
        monkeypatch.setattr(
            "app.services.file_tracker.parse_torrent_file_list",
            lambda _b: metas,
        )
        return service._sync_composition(  # type: ignore[attr-defined]
            normalized_hash=info_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=b"x",
        )

    # --- v1: первый торрент ---
    metas_v1 = [
        SimpleNamespace(relative_path=f1, size=1, file_index=0),
        SimpleNamespace(relative_path=f2, size=2, file_index=1),
        SimpleNamespace(relative_path=f3, size=3, file_index=2),
        SimpleNamespace(relative_path=f_dup, size=4, file_index=3),
    ]
    synced1 = sync(info_hash=v1, prior_hash=None, prior_files={}, metas=metas_v1)
    assert synced1.has_prior_version is False
    rows1 = rows_by_hash[v1]
    assert {r: rows1[r].ui_status for r in rows1} == {
        f1: UI_STATUS_NEW,
        f2: UI_STATUS_NEW,
        f3: UI_STATUS_NEW,
        f_dup: UI_STATUS_NEW,
    }
    assert {c.relative_path for c in persisted if c.kind == KIND_ADDED} == {
        f1,
        f2,
        f3,
        f_dup,
    }
    _assert_telegram_file_changes(
        release_id=release_id,
        torrent_id=torrent_id,
        composition_changes=list(persisted),
        expected={KIND_ADDED: {f1, f2, f3, f_dup}},
        baseline=True,
        monkeypatch=monkeypatch,
    )

    # --- v2: bump ---
    persisted.clear()
    metas_v2 = [
        SimpleNamespace(relative_path=f1, size=1, file_index=0),
        SimpleNamespace(relative_path=f2, size=2, file_index=1),
        SimpleNamespace(relative_path=f3, size=3, file_index=2),
        SimpleNamespace(relative_path=f4, size=4, file_index=3),
    ]
    synced2 = sync(
        info_hash=v2,
        prior_hash=v1,
        prior_files=dict(full_v1),
        metas=metas_v2,
    )
    assert synced2.has_prior_version is True
    rows2 = rows_by_hash[v2]
    assert rows2[f1].ui_status == UI_STATUS_OK
    assert rows2[f2].ui_status == UI_STATUS_OK
    assert rows2[f3].ui_status == UI_STATUS_OK  # до hash
    assert rows2[f4].ui_status == UI_STATUS_NEW
    assert f_dup not in rows2
    assert {c.relative_path for c in persisted if c.kind == KIND_ADDED} == {f4}
    assert {c.relative_path for c in persisted if c.kind == KIND_REMOVED} == {f_dup}

    # hash-settle v2: 1–2 match, 3 rebuilt
    FileTrackerService._settle_ui_status(
        rows2[f1], first_seen=False, mismatch=False, matched=True, is_baseline=False
    )
    FileTrackerService._settle_ui_status(
        rows2[f2], first_seen=False, mismatch=False, matched=True, is_baseline=False
    )
    FileTrackerService._settle_ui_status(
        rows2[f3], first_seen=False, mismatch=True, matched=False, is_baseline=False
    )
    FileTrackerService._settle_ui_status(
        rows2[f4], first_seen=True, mismatch=False, matched=False, is_baseline=False
    )
    assert rows2[f1].ui_status == UI_STATUS_OK
    assert rows2[f2].ui_status == UI_STATUS_OK
    assert rows2[f3].ui_status == UI_STATUS_CHANGED
    assert rows2[f4].ui_status == UI_STATUS_NEW
    _assert_telegram_file_changes(
        release_id=release_id,
        torrent_id=torrent_id,
        composition_changes=list(persisted),
        modified_paths={f3},
        expected={
            KIND_ADDED: {f4},
            KIND_REMOVED: {f_dup},
            KIND_MODIFIED: {f3},
        },
        baseline=False,
        monkeypatch=monkeypatch,
    )

    # повторный sync v2 — removed не дублируем
    before_removed = sum(1 for c in persisted if c.kind == KIND_REMOVED)
    sync(info_hash=v2, prior_hash=v1, prior_files=dict(full_v1), metas=metas_v2)
    after_removed = sum(1 for c in persisted if c.kind == KIND_REMOVED)
    assert after_removed == before_removed

    # --- v3: следующий bump; file__1 не появляется как removed ---
    persisted.clear()
    metas_v3 = [
        SimpleNamespace(relative_path=f1, size=1, file_index=0),
        SimpleNamespace(relative_path=f2, size=2, file_index=1),
        SimpleNamespace(relative_path=f3, size=3, file_index=2),
        SimpleNamespace(relative_path=f4, size=4, file_index=3),
        SimpleNamespace(relative_path=f5, size=5, file_index=4),
    ]
    synced3 = sync(
        info_hash=v3,
        prior_hash=v2,
        prior_files=dict(full_v2),
        metas=metas_v3,
    )
    assert synced3.has_prior_version is True
    rows3 = rows_by_hash[v3]
    assert rows3[f1].ui_status == UI_STATUS_OK
    assert rows3[f2].ui_status == UI_STATUS_OK
    assert rows3[f3].ui_status == UI_STATUS_OK
    assert rows3[f4].ui_status == UI_STATUS_OK
    assert rows3[f5].ui_status == UI_STATUS_NEW
    assert {c.relative_path for c in persisted if c.kind == KIND_ADDED} == {f5}
    assert {c.relative_path for c in persisted if c.kind == KIND_REMOVED} == set()

    FileTrackerService._settle_ui_status(
        rows3[f3], first_seen=False, mismatch=False, matched=True, is_baseline=False
    )
    FileTrackerService._settle_ui_status(
        rows3[f5], first_seen=True, mismatch=False, matched=False, is_baseline=False
    )
    assert rows3[f3].ui_status == UI_STATUS_OK
    assert rows3[f5].ui_status == UI_STATUS_NEW
    _assert_telegram_file_changes(
        release_id=release_id,
        torrent_id=torrent_id,
        composition_changes=list(persisted),
        expected={KIND_ADDED: {f5}},
        baseline=False,
        monkeypatch=monkeypatch,
    )

    # UI: removed file__1 только у v2
    from app.services.releases_view import _recent_events_by_info_hash
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    event_rows = [
        SimpleNamespace(
            id=1,
            release_id=release_id,
            torrent_id=torrent_id,
            info_hash=v2,
            kind=KIND_REMOVED,
            relative_path=f_dup,
            full_path=full_v1[f_dup],
            created_at=now,
        ),
        SimpleNamespace(
            id=2,
            release_id=release_id,
            torrent_id=torrent_id,
            info_hash=v2,
            kind=KIND_ADDED,
            relative_path=f4,
            full_path=full_v2[f4],
            created_at=now,
        ),
        SimpleNamespace(
            id=3,
            release_id=release_id,
            torrent_id=torrent_id,
            info_hash=v3,
            kind=KIND_ADDED,
            relative_path=f5,
            full_path=full_v3[f5],
            created_at=now,
        ),
    ]
    db_events = MagicMock()
    db_events.scalars.side_effect = [
        MagicMock(all=lambda: event_rows),
        MagicMock(all=lambda: []),  # archives legacy
    ]
    by_hash = _recent_events_by_info_hash(db_events, [release_id])
    assert (f_dup, full_v1[f_dup], "removed") in by_hash[v2].removed_candidates
    assert v3 in by_hash
    assert all(c[0] != f_dup for c in by_hash[v3].removed_candidates)


def test_seven_versions_incremental_and_folder_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Инкремент эпизодов + rename dir1→dir2: статусы по exact relative_path.

    v1: new dir1/file1
    v2: ok file1, new file2
    v3: ok file1–2, new file3
    v4: ok file1, changed file2, ok file3, new file4r
    v5: ok file1–3, removed file4r, new file4
    v6: rename папки — все dir2/* new, все dir1/* removed
    v7: ok dir2/file1–4, new file5; dir1 removed не повторяется
    TG: ➕/✏️/➖ по тем же путям; ok в сообщение не попадают.
    """
    from app.services.file_tracker import (
        KIND_ADDED,
        KIND_MODIFIED,
        KIND_REMOVED,
        FileTrackerService,
        TorrentFile,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    (media / "dir1").mkdir(parents=True)
    torrent_id = 99
    release_id = 11
    hashes = [f"{i:02x}" * 20 for i in range(1, 8)]
    v1, v2, v3, v4, v5, v6, v7 = hashes

    d1f1, d1f2, d1f3 = "dir1/file1.mkv", "dir1/file2.mkv", "dir1/file3.mkv"
    d1f4r, d1f4 = "dir1/file4r.mkv", "dir1/file4.mkv"
    d2f1, d2f2, d2f3, d2f4, d2f5 = (
        "dir2/file1.mkv",
        "dir2/file2.mkv",
        "dir2/file3.mkv",
        "dir2/file4.mkv",
        "dir2/file5.mkv",
    )

    def touch(rel: str, body: bytes) -> str:
        path = media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return str(path.resolve())

    def metas(*rels: str) -> list:
        return [
            SimpleNamespace(relative_path=rel, size=i + 1, file_index=i)
            for i, rel in enumerate(rels)
        ]

    def settle(row, *, mismatch: bool = False, matched: bool = False, first_seen: bool = False):
        FileTrackerService._settle_ui_status(
            row,
            first_seen=first_seen,
            mismatch=mismatch,
            matched=matched,
            is_baseline=False,
        )

    def tg(
        *,
        baseline: bool = False,
        expected: dict[str, set[str]],
        modified_paths: set[str] | None = None,
    ) -> None:
        _assert_telegram_file_changes(
            release_id=release_id,
            torrent_id=torrent_id,
            composition_changes=list(persisted),
            expected=expected,
            baseline=baseline,
            modified_paths=modified_paths,
            monkeypatch=monkeypatch,
        )

    rows_by_hash: dict[str, dict[str, object]] = {}
    persisted: list = []
    event_keys: set[tuple[str, str, str]] = set()

    def sync(
        *,
        info_hash: str,
        prior_hash: str | None,
        prior_files: dict[str, str],
        file_metas: list,
    ):
        persisted.clear()
        db = MagicMock()
        db.scalars.return_value.all.return_value = list(
            rows_by_hash.get(info_hash, {}).values()
        )

        def add(obj):
            if isinstance(obj, TorrentFile):
                rows_by_hash.setdefault(info_hash, {})[obj.relative_path] = obj

        db.add.side_effect = add
        service = FileTrackerService(db)
        monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
        monkeypatch.setattr(
            service, "_prior_version_files", lambda **_k: dict(prior_files)
        )

        def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
            out = []
            for change in changes:
                event_keys.add(
                    (
                        (info_hash or "").lower(),
                        change.kind,
                        change.relative_path or change.full_path or "",
                    )
                )
                persisted.append(change)
                out.append(SimpleNamespace(kind=change.kind, relative_path=change.relative_path))
            return out

        def has_prior(*, torrent_id, info_hash=None, kind, relative_path, full_path):  # noqa: ANN001
            return (
                (info_hash or "").lower(),
                kind,
                relative_path or full_path or "",
            ) in event_keys

        monkeypatch.setattr(service, "_persist_events", fake_persist)
        monkeypatch.setattr(service, "_has_prior_event", has_prior)
        monkeypatch.setattr(
            service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {})
        )
        monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
        monkeypatch.setattr(
            "app.services.file_tracker.resolve_full_path",
            lambda _base, rel, **_k: media / rel,
        )
        monkeypatch.setattr(
            "app.services.file_tracker.parse_torrent_file_list",
            lambda _b: file_metas,
        )
        return service._sync_composition(  # type: ignore[attr-defined]
            normalized_hash=info_hash,
            torrent_id=torrent_id,
            release_id=release_id,
            torrent_bytes=b"x",
        )

    def statuses(info_hash: str) -> dict[str, str]:
        return {rel: row.ui_status for rel, row in rows_by_hash[info_hash].items()}

    def kinds() -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for c in persisted:
            out.setdefault(c.kind, set()).add(c.relative_path)
        return out

    # v1
    full1 = {d1f1: touch(d1f1, b"1")}
    sync(info_hash=v1, prior_hash=None, prior_files={}, file_metas=metas(d1f1))
    assert statuses(v1) == {d1f1: UI_STATUS_NEW}
    assert kinds().get(KIND_ADDED) == {d1f1}
    tg(baseline=True, expected={KIND_ADDED: {d1f1}})

    # v2
    full2 = {d1f1: touch(d1f1, b"1"), d1f2: touch(d1f2, b"2")}
    sync(info_hash=v2, prior_hash=v1, prior_files=full1, file_metas=metas(d1f1, d1f2))
    settle(rows_by_hash[v2][d1f1], matched=True)
    settle(rows_by_hash[v2][d1f2], first_seen=True)
    assert statuses(v2) == {d1f1: UI_STATUS_OK, d1f2: UI_STATUS_NEW}
    assert kinds().get(KIND_ADDED) == {d1f2}
    assert KIND_REMOVED not in kinds()
    tg(expected={KIND_ADDED: {d1f2}})

    # v3
    full3 = {**full2, d1f3: touch(d1f3, b"3")}
    sync(
        info_hash=v3,
        prior_hash=v2,
        prior_files=full2,
        file_metas=metas(d1f1, d1f2, d1f3),
    )
    settle(rows_by_hash[v3][d1f1], matched=True)
    settle(rows_by_hash[v3][d1f2], matched=True)
    settle(rows_by_hash[v3][d1f3], first_seen=True)
    assert statuses(v3) == {
        d1f1: UI_STATUS_OK,
        d1f2: UI_STATUS_OK,
        d1f3: UI_STATUS_NEW,
    }
    assert kinds().get(KIND_ADDED) == {d1f3}
    tg(expected={KIND_ADDED: {d1f3}})

    # v4: file2 rebuilt, file4r new
    full4 = {
        d1f1: touch(d1f1, b"1"),
        d1f2: touch(d1f2, b"2-rebuilt"),
        d1f3: touch(d1f3, b"3"),
        d1f4r: touch(d1f4r, b"4r"),
    }
    sync(
        info_hash=v4,
        prior_hash=v3,
        prior_files=full3,
        file_metas=metas(d1f1, d1f2, d1f3, d1f4r),
    )
    settle(rows_by_hash[v4][d1f1], matched=True)
    settle(rows_by_hash[v4][d1f2], mismatch=True)
    settle(rows_by_hash[v4][d1f3], matched=True)
    settle(rows_by_hash[v4][d1f4r], first_seen=True)
    assert statuses(v4) == {
        d1f1: UI_STATUS_OK,
        d1f2: UI_STATUS_CHANGED,
        d1f3: UI_STATUS_OK,
        d1f4r: UI_STATUS_NEW,
    }
    assert kinds().get(KIND_ADDED) == {d1f4r}
    assert KIND_REMOVED not in kinds()
    tg(
        modified_paths={d1f2},
        expected={KIND_ADDED: {d1f4r}, KIND_MODIFIED: {d1f2}},
    )

    # v5: file4r → file4 (rename path = remove + add)
    full5 = {
        d1f1: touch(d1f1, b"1"),
        d1f2: touch(d1f2, b"2-rebuilt"),
        d1f3: touch(d1f3, b"3"),
        d1f4: touch(d1f4, b"4"),
    }
    sync(
        info_hash=v5,
        prior_hash=v4,
        prior_files=full4,
        file_metas=metas(d1f1, d1f2, d1f3, d1f4),
    )
    settle(rows_by_hash[v5][d1f1], matched=True)
    settle(rows_by_hash[v5][d1f2], matched=True)
    settle(rows_by_hash[v5][d1f3], matched=True)
    settle(rows_by_hash[v5][d1f4], first_seen=True)
    assert statuses(v5) == {
        d1f1: UI_STATUS_OK,
        d1f2: UI_STATUS_OK,
        d1f3: UI_STATUS_OK,
        d1f4: UI_STATUS_NEW,
    }
    assert kinds().get(KIND_ADDED) == {d1f4}
    assert kinds().get(KIND_REMOVED) == {d1f4r}
    tg(expected={KIND_ADDED: {d1f4}, KIND_REMOVED: {d1f4r}})

    # v6: rename folder dir1 → dir2 (все пути новые / все старые removed)
    full6 = {
        d2f1: touch(d2f1, b"1"),
        d2f2: touch(d2f2, b"2-rebuilt"),
        d2f3: touch(d2f3, b"3"),
        d2f4: touch(d2f4, b"4"),
    }
    sync(
        info_hash=v6,
        prior_hash=v5,
        prior_files=full5,
        file_metas=metas(d2f1, d2f2, d2f3, d2f4),
    )
    for rel in (d2f1, d2f2, d2f3, d2f4):
        settle(rows_by_hash[v6][rel], first_seen=True)
    assert statuses(v6) == {
        d2f1: UI_STATUS_NEW,
        d2f2: UI_STATUS_NEW,
        d2f3: UI_STATUS_NEW,
        d2f4: UI_STATUS_NEW,
    }
    assert kinds().get(KIND_ADDED) == {d2f1, d2f2, d2f3, d2f4}
    assert kinds().get(KIND_REMOVED) == {d1f1, d1f2, d1f3, d1f4}
    tg(
        expected={
            KIND_ADDED: {d2f1, d2f2, d2f3, d2f4},
            KIND_REMOVED: {d1f1, d1f2, d1f3, d1f4},
        }
    )

    # v7: +file5; dir1 removed не повторяется
    sync(
        info_hash=v7,
        prior_hash=v6,
        prior_files=full6,
        file_metas=metas(d2f1, d2f2, d2f3, d2f4, d2f5),
    )
    for rel in (d2f1, d2f2, d2f3, d2f4):
        settle(rows_by_hash[v7][rel], matched=True)
    settle(rows_by_hash[v7][d2f5], first_seen=True)
    assert statuses(v7) == {
        d2f1: UI_STATUS_OK,
        d2f2: UI_STATUS_OK,
        d2f3: UI_STATUS_OK,
        d2f4: UI_STATUS_OK,
        d2f5: UI_STATUS_NEW,
    }
    assert kinds().get(KIND_ADDED) == {d2f5}
    assert KIND_REMOVED not in kinds()
    assert not any(
        c.relative_path in {d1f1, d1f2, d1f3, d1f4, d1f4r} and c.kind == KIND_REMOVED
        for c in persisted
    )
    tg(expected={KIND_ADDED: {d2f5}})


def test_sync_composition_baseline_preserves_sticky_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чистый baseline re-sync не затирает sticky changed → new."""
    from app.services.file_tracker import (
        FileTrackerService,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
    )

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "cc" * 20
    rel = "Show/ep01.mkv"
    existing = SimpleNamespace(
        relative_path=rel,
        torrent_id=1,
        release_id=1,
        size=1,
        file_index=0,
        selected=True,
        full_path=str(media / rel),
        ui_status=UI_STATUS_CHANGED,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [existing]
    db.scalar.return_value = None

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_has_prior_event", lambda **_k: True)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda *_a, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [SimpleNamespace(relative_path=rel, size=1, file_index=0)],
    )
    monkeypatch.setattr(
        service, "_load_hashed_canonical_paths", lambda _paths: set()
    )

    service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=1,
        torrent_bytes=b"x",
    )
    assert existing.ui_status == UI_STATUS_CHANGED
    assert existing.ui_status != UI_STATUS_NEW


def test_track_lv999_version_bump_hash_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После hash: [04] пересобран → changed; [01–03] → ok; [06]/[05](1) → sticky new."""
    from app.services.file_tracker import (
        KIND_MODIFIED,
        TrackTorrentResult,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    media = tmp_path / "anilibria"
    (media / "Show").mkdir(parents=True)
    info_hash = "n2" * 20
    prior_hash = "o2" * 20

    ep01, ep02, ep03, ep04 = "Show/ep01.mkv", "Show/ep02.mkv", "Show/ep03.mkv", "Show/ep04.mkv"
    ep05_renamed, ep06 = "Show/ep05 (1).mkv", "Show/ep06.mkv"

    rows_by_rel: dict[str, SimpleNamespace] = {}
    for rel, status in (
        (ep01, UI_STATUS_OK),
        (ep02, UI_STATUS_OK),
        (ep03, UI_STATUS_OK),
        (ep04, UI_STATUS_OK),
        (ep05_renamed, UI_STATUS_NEW),
        (ep06, UI_STATUS_NEW),
    ):
        full = media / rel
        full.write_bytes(b"body:" + rel.encode())
        rows_by_rel[rel] = SimpleNamespace(
            relative_path=rel,
            selected=True,
            full_path=str(full.resolve()),
            ui_status=status,
        )

    prior_hashes = {ep01: "h01", ep02: "h02", ep03: "h03", ep04: "h04-old"}
    new_hashes = {
        rows_by_rel[ep01].full_path: "h01",
        rows_by_rel[ep02].full_path: "h02",
        rows_by_rel[ep03].full_path: "h03",
        rows_by_rel[ep04].full_path: "h04-new",
        rows_by_rel[ep05_renamed].full_path: "h05b",
        rows_by_rel[ep06].full_path: "h06",
    }

    db = MagicMock()
    db.scalars.return_value.all.return_value = list(rows_by_rel.values())

    def scalar_for_path(stmt=None, **_k):  # noqa: ANN001
        try:
            params = list((stmt.compile().params or {}).values())
        except Exception:
            params = []
        for val in params:
            if isinstance(val, str) and val in new_hashes:
                return SimpleNamespace(content_hash=new_hashes[val], full_path=val)
        return None

    db.scalar.side_effect = scalar_for_path

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_hash)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: dict(prior_hashes))
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[],
            has_prior_version=True,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={ep05_renamed, ep06},
            baseline_had_known=False,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 6, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(
        service,
        "_filter_duplicate_changes",
        lambda **kwargs: list(kwargs.get("changes") or []),
    )
    persisted: list = []

    def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
        persisted.extend(changes)
        return [
            SimpleNamespace(kind=c.kind, relative_path=c.relative_path, id=i)
            for i, c in enumerate(changes, start=1)
        ]

    monkeypatch.setattr(service, "_persist_events", fake_persist)
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    notified: list = []
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **kw: notified.append(kw))

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=True)

    assert rows_by_rel[ep01].ui_status == UI_STATUS_OK
    assert rows_by_rel[ep02].ui_status == UI_STATUS_OK
    assert rows_by_rel[ep03].ui_status == UI_STATUS_OK
    assert rows_by_rel[ep04].ui_status == UI_STATUS_CHANGED
    assert rows_by_rel[ep05_renamed].ui_status == UI_STATUS_NEW
    assert rows_by_rel[ep06].ui_status == UI_STATUS_NEW
    assert {c.relative_path for c in persisted if c.kind == KIND_MODIFIED} == {ep04}
    assert notified and notified[0].get("baseline") is False


def test_sync_composition_heals_added_when_rows_exist_without_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory успел записать torrent_files без events → heal added; baseline ok не трогаем."""
    from app.services.file_tracker import KIND_ADDED

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "cd" * 20
    rel = str(Path("Show Name") / "ep01.mkv")
    full = media / rel
    full.parent.mkdir(parents=True)
    full.write_bytes(b"exists")
    existing = SimpleNamespace(
        relative_path=rel,
        torrent_id=1,
        release_id=1,
        size=1,
        file_index=0,
        selected=True,
        full_path=str(full),
        ui_status="ok",
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [existing]
    # _prior_version_hash → None; per-row _has_prior_event → None
    db.scalar.side_effect = [None, None]

    service = FileTrackerService(db)
    persisted: list[FileChange] = []

    def fake_persist(*, release_id, torrent_id, changes, info_hash=None):  # noqa: ANN001
        persisted.extend(changes)
        return [SimpleNamespace(kind=c.kind) for c in changes]

    monkeypatch.setattr(service, "_persist_events", fake_persist)
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda *_a, **_k: full,
    )
    # Только один файл из multi — подменим parse
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [SimpleNamespace(relative_path=rel, size=100, file_index=0)],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=9,
        release_id=3,
        torrent_bytes=b"unused",
    )
    assert synced.has_prior_version is False
    assert len(persisted) == 1
    assert persisted[0].kind == KIND_ADDED
    assert persisted[0].relative_path == rel
    # Первый торрент: inventory ok → чиним в new (добавление этой версии).
    assert existing.ui_status == "new"


def test_mark_master_added_triggers_composition_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    pipeline = SimpleNamespace(
        id=1,
        status="discovered",
        info_hash="ef" * 20,
        torrent_id=5,
        release_id=8,
        master_added_at=None,
        error="x",
    )
    service = TorrentPipelineService(db)
    service._add_log = MagicMock()  # type: ignore[method-assign]
    called: dict[str, object] = {}

    class FakeTracker:
        def __init__(self, _db):
            pass

        def sync_torrent_composition(self, **kwargs):
            called.update(kwargs)
            return SimpleNamespace(
                skipped_reason=None,
                files_upserted=2,
                changes=[
                    FileChange(kind="added", relative_path="a.mkv"),
                    FileChange(kind="added", relative_path="b.mkv"),
                ],
            )

    monkeypatch.setattr(
        "app.services.file_tracker.FileTrackerService",
        FakeTracker,
    )
    service.load_torrent_bytes_from_archive = MagicMock(return_value=b"torrent")  # type: ignore[method-assign]

    service.mark_master_added(pipeline)

    assert pipeline.status == TorrentPipelineService.STATUS_MASTER_ADDED
    assert called["info_hash"] == pipeline.info_hash
    assert called["torrent_id"] == 5
    assert called["notify"] is False
    assert any("sync состава" in str(c) for c in service._add_log.call_args_list)


def test_filter_duplicate_changes_skips_repeated_missing() -> None:
    db = MagicMock()
    service = FileTrackerService(db)
    db.scalar.side_effect = [object(), None]

    filtered = service._filter_duplicate_changes(  # type: ignore[attr-defined]
        torrent_id=10,
        info_hash="aa" * 20,
        changes=[
            FileChange(kind="missing", relative_path="Show/ep01.mkv", full_path="/anilibria/Show/ep01.mkv"),
            FileChange(kind="added", relative_path="Show/ep02.mkv", full_path="/anilibria/Show/ep02.mkv"),
        ],
    )

    assert [item.kind for item in filtered] == ["added"]


def test_build_file_changes_notification_text() -> None:
    text = build_file_changes_notification_text(
        title="Тест",
        alias="test-show",
        torrent_label="HEVC · 1-12",
        changes=[
            {"kind": "added", "relative_path": "Show/ep01.mkv"},
            {"kind": "removed", "relative_path": "Show/ep00.mkv"},
            {"kind": "modified", "relative_path": "Show/ep02.mkv"},
            {"kind": "missing", "relative_path": "Show/ep03.mkv"},
        ],
    )
    assert "Изменения файлов" in text
    assert "test\\-show" in text or "test-show" in text.replace("\\", "")
    assert "➕" in text
    assert "➖" in text
    assert "✏️" in text
    assert "⚠️" in text
    assert "содержимое" in text
    assert "нет на диске" in text


def test_build_file_changes_baseline_notification_text() -> None:
    text = build_file_changes_notification_text(
        title="Тест",
        alias="test-show",
        torrent_label="HEVC · 1-12",
        changes=[
            {"kind": "added", "relative_path": "Show/ep01.mkv"},
            {"kind": "added", "relative_path": "Show/ep02.mkv"},
        ],
        baseline=True,
    )
    assert "Файлы торрента добавлены в базу" in text
    assert "Файлов: `2`" in text
    assert "Изменения файлов" not in text
    assert "➕" in text


def test_split_active_archived() -> None:
    rows = [
        ReleaseTorrentRow(
            archive_id=1,
            torrent_id=1,
            info_hash="a" * 40,
            torrent_type="HEVC",
            torrent_description="1-12",
            file_size=1,
            file_size_label="1 B",
            created_at=None,
            pipeline_status=None,
            pipeline_error=None,
            api_present=True,
        ),
        ReleaseTorrentRow(
            archive_id=2,
            torrent_id=2,
            info_hash="b" * 40,
            torrent_type="AVC",
            torrent_description="1-10",
            file_size=1,
            file_size_label="1 B",
            created_at=None,
            pipeline_status=None,
            pipeline_error=None,
            api_present=False,
        ),
    ]
    active, archived = split_active_archived(rows)
    assert len(active) == 1 and active[0].torrent_id == 1
    assert len(archived) == 1 and archived[0].torrent_id == 2


def test_same_sticky_rip_family_fail_closed_when_current_codec_unknown() -> None:
    """Без codec текущего — sibling с AVC/HEVC не prior (fail closed)."""
    from types import SimpleNamespace

    hevc = SimpleNamespace(
        quality_json={
            "type": {"value": "BDRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "HEVC"},
        },
        torrent_type="BDRip 1080p HEVC",
    )
    unknown = SimpleNamespace(quality_json={}, torrent_type=None)
    assert (
        FileTrackerService._same_sticky_rip_family(
            current_codec=None,
            current_family="BDRip 1080p",
            candidate=hevc,
        )
        is False
    )
    assert (
        FileTrackerService._same_sticky_rip_family(
            current_codec="AVC",
            current_family="BDRip 1080p",
            candidate=hevc,
        )
        is False
    )
    assert (
        FileTrackerService._same_sticky_rip_family(
            current_codec="HEVC",
            current_family="BDRip 1080p",
            candidate=hevc,
        )
        is True
    )
    # Известный current + неизвестный candidate — fail closed.
    assert (
        FileTrackerService._same_sticky_rip_family(
            current_codec="AVC",
            current_family="BDRip 1080p",
            candidate=unknown,
        )
        is False
    )
    # Оба без codec — семейство решают family-ключи.
    assert (
        FileTrackerService._same_sticky_rip_family(
            current_codec=None,
            current_family="",
            candidate=unknown,
        )
        is True
    )


def test_file_status_for_ui_sticky_semantics() -> None:
    from app.services.file_tracker import file_status_for_ui

    # Sticky new — не зависит от диска
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            ui_status="new",
            in_torrent=True,
        )
        == "new"
    )
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            ui_status="ok",
            in_torrent=True,
        )
        == "ok"
    )
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            ui_status="changed",
            in_torrent=True,
        )
        == "changed"
    )
    assert (
        file_status_for_ui(
            relative_path="old.mkv",
            full_path="/media/old.mkv",
            in_torrent=False,
        )
        == "removed"
    )
    # Fallback на latest_kind, если ui_status пуст
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            latest_kind="added",
            in_torrent=True,
        )
        == "new"
    )
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            latest_kind="modified",
            in_torrent=True,
        )
        == "changed"
    )
    # checking — временный оверлей
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            ui_status="ok",
            hash_job_active=True,
            in_torrent=True,
        )
        == "checking"
    )
    # incremental new без prior hash — остаётся new даже при active job
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path="/media/p.mkv",
            ui_status="new",
            disk_hash=None,
            hash_job_active=True,
            in_torrent=True,
        )
        == "new"
    )


def test_file_status_for_ui_partial_qb_is_checking(tmp_path: Path) -> None:
    """ok/changed + только .!qB на диске → проверка; new остаётся new."""
    from app.services.file_tracker import file_status_for_ui

    media = tmp_path / "show"
    media.mkdir()
    complete = media / "ep01.mkv"
    Path(str(complete) + ".!qB").write_bytes(b"partial")

    assert (
        file_status_for_ui(
            relative_path="ep01.mkv",
            full_path=str(complete),
            ui_status="ok",
        )
        == "checking"
    )
    assert (
        file_status_for_ui(
            relative_path="ep01.mkv",
            full_path=str(complete),
            ui_status="changed",
        )
        == "checking"
    )
    assert (
        file_status_for_ui(
            relative_path="ep01.mkv",
            full_path=str(complete),
            ui_status="new",
        )
        == "new"
    )
    # Соседний .!qB при уже complete-файле не даёт checking
    complete.write_bytes(b"done")
    assert (
        file_status_for_ui(
            relative_path="ep01.mkv",
            full_path=str(complete),
            ui_status="ok",
        )
        == "ok"
    )


def test_settle_ui_status_rules() -> None:
    from app.services.file_tracker import (
        FileTrackerService,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    # Подлинный new (first_seen) — финальный, не понижается даже при matched
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(row, first_seen=True, mismatch=False, matched=True)
    assert row.ui_status == UI_STATUS_NEW

    # Ложный sticky new (путь был в prior) + mismatch → changed
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=True, matched=False)
    assert row.ui_status == UI_STATUS_CHANGED

    # Ложный sticky new + matched → ok
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=False, matched=True)
    assert row.ui_status == UI_STATUS_OK

    # Ложный sticky new без hash-данных → provisional ok (путь из prior)
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=False, matched=False)
    assert row.ui_status == UI_STATUS_OK

    # first_seen=False без path_in_prior (пустой prior) → new не лечим
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(
        row, first_seen=False, mismatch=False, matched=False, path_in_prior=False
    )
    assert row.ui_status == UI_STATUS_NEW

    # changed — финальный, не понижается
    row.ui_status = UI_STATUS_CHANGED
    FileTrackerService._settle_ui_status(row, first_seen=True, mismatch=False, matched=True)
    assert row.ui_status == UI_STATUS_CHANGED

    # ok + first_seen → new (файла не было в прошлой версии)
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(row, first_seen=True, mismatch=False, matched=False)
    assert row.ui_status == UI_STATUS_NEW

    # ok + mismatch → changed
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=True, matched=False)
    assert row.ui_status == UI_STATUS_CHANGED

    # ok + matched → ok
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=False, matched=True)
    assert row.ui_status == UI_STATUS_OK

    # нет данных для сравнения → статус не меняем (существовавший файл уже создан как ok)
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=False, matched=False)
    assert row.ui_status == UI_STATUS_OK

    # нет данных, пустой статус → остаётся как есть (не фабрикуем ok)
    row.ui_status = ""
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=False, matched=False)
    assert row.ui_status == ""

    # baseline: «новый» остаётся new (добавление первого торрента)
    row.ui_status = UI_STATUS_NEW
    FileTrackerService._settle_ui_status(
        row, first_seen=True, mismatch=False, matched=False, is_baseline=True
    )
    assert row.ui_status == UI_STATUS_NEW

    # baseline + disk mismatch ≠ changed (нет prior — только new)
    row.ui_status = UI_STATUS_NEW
    FileTrackerService._settle_ui_status(
        row, first_seen=False, mismatch=True, matched=False, is_baseline=True
    )
    assert row.ui_status == UI_STATUS_NEW

    # baseline legacy ok без prior — не повышаем в changed с диска
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(
        row, first_seen=False, mismatch=True, matched=False, is_baseline=True
    )
    assert row.ui_status == UI_STATUS_OK


def test_sync_composition_status_by_prior_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новый файл vs прошлая версия → «новый»; существовавший → «ok» (хеш уточнит)."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "33" * 20
    ep01 = "Show Name/ep01.mkv"  # был в прошлой версии
    ep02 = "Show Name/ep02.mkv"  # новый

    db = MagicMock()
    db.scalars.return_value.all.return_value = []  # для этого info_hash строк ещё нет
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: "prev")
    monkeypatch.setattr(service, "_prior_version_files", lambda **_k: {ep01: None})
    persisted: list[FileChange] = []
    monkeypatch.setattr(
        service,
        "_persist_events",
        lambda *, release_id, torrent_id, changes, info_hash=None: persisted.extend(changes) or [],
    )
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=ep01, size=1, file_index=0),
            SimpleNamespace(relative_path=ep02, size=2, file_index=1),
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )

    assert synced.has_prior_version is True
    assert synced.first_seen_paths == {ep02}
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows[ep01].ui_status == UI_STATUS_OK
    assert rows[ep02].ui_status == UI_STATUS_NEW
    # added-событие только на реально новый файл
    assert [c.relative_path for c in persisted] == [ep02]


def _run_track_settle(
    tmp_path,
    monkeypatch,
    *,
    initial_status: str,
    first_seen: bool,
    has_prior: bool,
    prev_hash: object,
    new_hash: str,
    events: list,
):
    """Хелпер: прогнать track_torrent над одним файлом и вернуть (row, notified)."""
    from app.services.file_tracker import TrackTorrentResult

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "44" * 20
    rel = "Show/ep01.mkv"
    full = media / rel
    full.parent.mkdir(parents=True)
    full.write_bytes(b"data")

    file_row = SimpleNamespace(
        relative_path=rel, selected=True, full_path=str(full), ui_status=initial_status
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [file_row]
    # DiskFileHash после хеширования (prior hashes — отдельно через _prior_version_hashes).
    db.scalar.side_effect = lambda *a, **k: SimpleNamespace(content_hash=new_hash)

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash, torrent_bytes=b"x", archive=None, skipped_reason=None
        ),
    )
    prior_info = "33" * 20 if has_prior else None
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: prior_info)
    prior_hashes = {}
    if has_prior and prev_hash is not None:
        content = getattr(prev_hash, "content_hash", None) or ""
        if content:
            prior_hashes = {rel: content}
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: prior_hashes)
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(
                has_prior_version=has_prior,
                prior_info_hash=prior_info,
            ),
            events=list(events),
            has_prior_version=has_prior,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rel} if first_seen else set(),
            baseline_had_known=False,
            prior_info_hash=prior_info,
            prior_archive_id=None,
            ui_transitions=[],
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 1, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_emit_ui_status_pipeline_event", lambda **_k: None)
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    notified: list[dict] = []
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **kw: notified.append(kw))

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=True)
    return file_row, notified


def test_track_new_version_unchanged_file_ok(tmp_path, monkeypatch) -> None:
    """Новая версия, файл был раньше и хеш совпал → ok."""
    from app.services.file_tracker import UI_STATUS_OK

    row, _ = _run_track_settle(
        tmp_path,
        monkeypatch,
        initial_status="ok",
        first_seen=False,
        has_prior=True,
        prev_hash=SimpleNamespace(content_hash="abc"),
        new_hash="abc",
        events=[],
    )
    assert row.ui_status == UI_STATUS_OK


def test_track_new_version_changed_file_changed(tmp_path, monkeypatch) -> None:
    """Новая версия, хеш разошёлся с прошлой → изменён (финально)."""
    from app.services.file_tracker import UI_STATUS_CHANGED

    row, _ = _run_track_settle(
        tmp_path,
        monkeypatch,
        initial_status="ok",
        first_seen=False,
        has_prior=True,
        prev_hash=SimpleNamespace(content_hash="abc"),
        new_hash="xyz",
        events=[],
    )
    assert row.ui_status == UI_STATUS_CHANGED


def test_track_first_ever_file_stays_new(tmp_path, monkeypatch) -> None:
    """Первый торрент релиза: после hash-settle остаётся new (добавление версии)."""
    from app.services.file_tracker import UI_STATUS_NEW

    row, notified = _run_track_settle(
        tmp_path,
        monkeypatch,
        initial_status="new",
        first_seen=True,
        has_prior=False,
        prev_hash=None,
        new_hash="abc",
        events=[SimpleNamespace(kind="added", relative_path="Show/ep01.mkv")],
    )
    assert row.ui_status == UI_STATUS_NEW
    assert len(notified) == 1
    assert notified[0]["baseline"] is True


def test_track_baseline_incomplete_keeps_status(tmp_path, monkeypatch) -> None:
    """Baseline settle: .!qB не хешируем; sticky new/ok не трогаем принудительно."""
    from app.services.file_tracker import TrackTorrentResult, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "55" * 20
    rel = "Show/ep01.mkv"
    full = media / rel
    full.parent.mkdir(parents=True)
    Path(str(full) + ".!qB").write_bytes(b"partial")

    file_row = SimpleNamespace(
        relative_path=rel, selected=True, full_path=str(full), ui_status=UI_STATUS_NEW
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [file_row]
    db.scalar.return_value = None

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash, torrent_bytes=b"x", archive=None, skipped_reason=None
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[SimpleNamespace(kind="added", relative_path=rel)],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rel},
            baseline_had_known=False,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    hashed: list = []
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda paths, *_a, **_k: hashed.extend(paths)
        or {"hashed": 0, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **_k: None)

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=False)
    assert hashed == []
    assert file_row.ui_status == UI_STATUS_NEW


def test_baseline_provisional_clean_always_new_mixed_uses_hash(tmp_path: Path) -> None:
    """Clean baseline → всегда new; mixed: хэш → ok, без хэша → new."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    full = media / "Show" / "ep.mkv"
    full.parent.mkdir(parents=True)
    Path(str(full) + ".!qB").write_bytes(b"partial")

    service = FileTrackerService(MagicMock())
    # Clean: даже с хэшем в БД — new (нет prior-версии для сравнения)
    assert (
        service._baseline_provisional_status(str(full), hashed_paths={str(full)})
        == UI_STATUS_NEW
    )
    assert (
        service._baseline_provisional_status(str(full), hashed_paths=set())
        == UI_STATUS_NEW
    )
    qb_path = str(full) + ".!qB"
    assert (
        service._baseline_provisional_status(qb_path, hashed_paths={str(full)})
        == UI_STATUS_NEW
    )
    # Mixed: хэш → ok; без хэша → new
    assert (
        service._baseline_provisional_status(
            str(full), hashed_paths={str(full)}, mixed=True
        )
        == UI_STATUS_OK
    )
    assert (
        service._baseline_provisional_status(
            qb_path, hashed_paths=set(), mixed=True
        )
        == UI_STATUS_NEW
    )


def test_baseline_provisional_mixed_complete_without_hash_is_new(tmp_path: Path) -> None:
    """Mixed=True: complete без хеша → new; clean → всегда new."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    known = media / "Show" / "ep01.mkv"
    newbie = media / "Show" / "ep03.mkv"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"old")
    newbie.write_bytes(b"fresh")

    service = FileTrackerService(MagicMock())
    hashed = {str(known)}
    assert (
        service._baseline_provisional_status(str(known), hashed_paths=hashed, mixed=True)
        == UI_STATUS_OK
    )
    assert (
        service._baseline_provisional_status(
            str(newbie), hashed_paths=hashed, mixed=True
        )
        == UI_STATUS_NEW
    )
    assert (
        service._baseline_provisional_status(
            str(newbie), hashed_paths=hashed, mixed=False
        )
        == UI_STATUS_NEW
    )


def test_sync_composition_keeps_baseline_new_when_hash_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Первый торрент: даже с content_hash в БД статус остаётся new."""
    from app.services.file_tracker import UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "66" * 20
    rel_a = "Show/ep01.mkv"
    rel_b = "Show/ep02.mkv"
    full_a = str(media / rel_a)
    full_b = str(media / rel_b)

    row_a = SimpleNamespace(
        relative_path=rel_a,
        torrent_id=1,
        release_id=1,
        size=1,
        file_index=0,
        selected=True,
        full_path=full_a,
        ui_status=UI_STATUS_NEW,
    )
    row_b = SimpleNamespace(
        relative_path=rel_b,
        torrent_id=1,
        release_id=1,
        size=2,
        file_index=1,
        selected=True,
        full_path=full_b,
        ui_status=UI_STATUS_NEW,
    )

    scalars_calls: list = []

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    def fake_scalars(stmt):  # noqa: ANN001
        scalars_calls.append(stmt)
        n = len(scalars_calls)
        if n == 1:
            return FakeScalars([row_a, row_b])
        return FakeScalars([full_a, full_b])

    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.scalar.side_effect = [None, None]

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel_a, size=1, file_index=0),
            SimpleNamespace(relative_path=rel_b, size=2, file_index=1),
        ],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    assert row_a.ui_status == UI_STATUS_NEW
    assert row_b.ui_status == UI_STATUS_NEW
    # previous files + hashed_paths lookup (prior замокан)
    assert len(scalars_calls) == 2


def test_sync_composition_mixed_keeps_sticky_new_after_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed baseline: ok+new, у new уже есть хэш → re-sync не лечит в ok."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "77" * 20
    rel_a = "Show/ep01.mkv"
    rel_b = "Show/ep03.mkv"
    full_a = str(media / rel_a)
    full_b = str(media / rel_b)
    (media / "Show").mkdir(parents=True)
    Path(full_a).write_bytes(b"a")
    Path(full_b).write_bytes(b"b")

    row_a = SimpleNamespace(
        relative_path=rel_a,
        torrent_id=1,
        release_id=1,
        size=1,
        file_index=0,
        selected=True,
        full_path=full_a,
        ui_status=UI_STATUS_OK,
    )
    row_b = SimpleNamespace(
        relative_path=rel_b,
        torrent_id=1,
        release_id=1,
        size=2,
        file_index=1,
        selected=True,
        full_path=full_b,
        ui_status=UI_STATUS_NEW,
    )

    db = MagicMock()
    db.scalars.return_value.all.return_value = [row_a, row_b]
    db.scalar.return_value = None

    service = FileTrackerService(db)
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.resolve_full_path",
        lambda _base, rel, **_k: media / rel,
    )
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [
            SimpleNamespace(relative_path=rel_a, size=1, file_index=0),
            SimpleNamespace(relative_path=rel_b, size=2, file_index=1),
        ],
    )
    monkeypatch.setattr(
        service,
        "_load_hashed_canonical_paths",
        lambda _paths: {full_a, full_b},
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    assert synced.baseline_had_known is True
    assert row_a.ui_status == UI_STATUS_OK
    assert row_b.ui_status == UI_STATUS_NEW


def test_track_mixed_baseline_e2e_preserves_new_after_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e2e track_torrent: baseline_had_known + ep03 new → после hash settle остаётся new."""
    from app.services.file_tracker import TrackTorrentResult, UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "e2" * 20
    rows: list[SimpleNamespace] = []
    for idx, name in enumerate(("ep01.mkv", "ep02.mkv", "ep03.mkv")):
        rel = f"Show/{name}"
        full = media / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"data")
        rows.append(
            SimpleNamespace(
                relative_path=rel,
                selected=True,
                full_path=str(full),
                ui_status=UI_STATUS_NEW if idx == 2 else UI_STATUS_OK,
            )
        )
    ep03 = rows[2].relative_path

    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    db.scalar.return_value = SimpleNamespace(content_hash="old")

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={ep03},
            baseline_had_known=True,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 3, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **_k: None)

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=False)
    assert rows[0].ui_status == UI_STATUS_OK
    assert rows[1].ui_status == UI_STATUS_OK
    assert rows[2].ui_status == UI_STATUS_NEW


def test_track_partial_backfill_e2e_keeps_all_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e2e: первый торрент (baseline_had_known=False) → после hash все остаются new."""
    from app.services.file_tracker import TrackTorrentResult, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "e3" * 20
    rows: list[SimpleNamespace] = []
    for name in ("ep01.mkv", "ep02.mkv", "ep03.mkv"):
        rel = f"Show/{name}"
        full = media / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"data")
        rows.append(
            SimpleNamespace(
                relative_path=rel,
                selected=True,
                full_path=str(full),
                ui_status="new",
            )
        )

    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    db.scalar.return_value = SimpleNamespace(content_hash="h")

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rows[2].relative_path},
            baseline_had_known=False,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 3, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **_k: None)

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=False)
    assert all(r.ui_status == UI_STATUS_NEW for r in rows)


def test_prepare_track_prefers_exact_info_hash() -> None:
    """Не берём активную другую версию torrent_id вместо точного info_hash."""
    old = SimpleNamespace(info_hash="aa" * 20, torrent_id=7, api_present=False, superseded=True)
    db = MagicMock()
    # Первый scalar — exact hash (superseded, api_present=False) → skip
    db.scalar.return_value = old

    service = FileTrackerService(db)
    prepared = service._prepare_track(  # type: ignore[attr-defined]
        info_hash="aa" * 20,
        torrent_id=7,
        torrent_bytes=None,
    )
    assert prepared.skipped_reason == "торрент не api_present (архивный)"
    assert prepared.archive is old
    # Не должно быть второго lookup по torrent_id
    assert db.scalar.call_count == 1


def test_prior_version_hash_selection() -> None:
    """Самый свежий prior с составом; без кандидатов → None; uppercase → lower."""
    db = MagicMock()
    calls = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    empty = SimpleNamespace(id=3, info_hash="cc" * 20)
    with_files_row = SimpleNamespace(id=2, info_hash="BB" + "b" * 38)

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            # newest first: empty composition, then one with files
            return FakeScalars([empty, with_files_row])
        if calls["n"] == 2:
            return FakeScalars(["BB" + "b" * 38])  # только у этого есть torrent_files
        return FakeScalars([])

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    prior = service._prior_version_hash(torrent_id=7, current_hash="aa" * 20)  # type: ignore[attr-defined]
    assert prior == ("bb" + "b" * 38)

    calls["n"] = 0
    db.scalars.side_effect = fake_scalars
    archive = service._prior_version_archive(torrent_id=7, current_hash="aa" * 20)  # type: ignore[attr-defined]
    assert archive is not None
    assert archive.id == 2
    assert (archive.info_hash or "").lower() == ("bb" + "b" * 38)

    db2 = MagicMock()
    db2.scalars.return_value.all.return_value = []
    service2 = FileTrackerService(db2)
    assert service2._prior_version_hash(torrent_id=7, current_hash="aa" * 20) is None  # type: ignore[attr-defined]


def test_prior_version_hashes_single_in_query() -> None:
    """content_hash прошлой версии собираются одним IN-запросом (без N+1)."""
    db = MagicMock()
    prior_files = [
        SimpleNamespace(relative_path="a.mkv", full_path="/m/a.mkv"),
        SimpleNamespace(relative_path="b.mkv", full_path="/m/b.mkv"),
        SimpleNamespace(relative_path="c.mkv", full_path=None),  # без пути — пропуск
    ]
    disk = [
        SimpleNamespace(full_path="/m/a.mkv", content_hash="h1"),
        SimpleNamespace(full_path="/m/b.mkv", content_hash="h2"),
    ]
    calls = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    def fake_scalars(_stmt):
        calls["n"] += 1
        return FakeScalars(prior_files if calls["n"] == 1 else disk)

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    result = service._prior_version_hashes(prior_hash="bb" * 20)  # type: ignore[attr-defined]
    assert result == {"a.mkv": "h1", "b.mkv": "h2"}
    # ровно 2 запроса scalars: TorrentFile + DiskFileHash IN (не N+1)
    assert calls["n"] == 2


def test_sync_composition_prior_archive_but_lost_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прошлая версия есть (архив), но состав потерян → не лечим в ok, держим new."""
    from app.services.file_tracker import KIND_ADDED, TorrentFile, UI_STATUS_NEW

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "55" * 20
    rel = "Show/ep01.mkv"

    db = MagicMock()
    db.scalars.return_value.all.return_value = []  # нет строк текущей версии
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    service = FileTrackerService(db)
    # Прошлая версия существует, но её состав пуст (потерян).
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: "44" * 20)
    monkeypatch.setattr(service, "_prior_version_files", lambda **_k: {})
    persisted: list[FileChange] = []
    monkeypatch.setattr(
        service,
        "_persist_events",
        lambda *, release_id, torrent_id, changes, info_hash=None: persisted.extend(changes) or [],
    )
    monkeypatch.setattr(service, "_qb_paths_and_priorities", lambda _h: (str(media), str(media), {}))
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr("app.services.file_tracker.resolve_full_path", lambda base, r, **_k: media / r)
    monkeypatch.setattr(
        "app.services.file_tracker.parse_torrent_file_list",
        lambda _b: [SimpleNamespace(relative_path=rel, size=1, file_index=0)],
    )

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    # Архив прошлой версии есть → это НЕ baseline (никакой сводки «файлы в базе»).
    assert synced.has_prior_version is True
    # Состав неизвестен → не лечим в ok; sticky new до появления состава prior.
    assert synced.first_seen_paths == {rel}
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert rows[0].ui_status == UI_STATUS_NEW
    assert [c.kind for c in persisted] == [KIND_ADDED]


def test_maybe_notify_baseline_skips_missing() -> None:
    from app.services.file_tracker import KIND_ADDED, KIND_MISSING

    db = MagicMock()
    db.get.return_value = SimpleNamespace(enabled=True)
    service = FileTrackerService(db)
    notified: list = []

    def fake_enqueue(db, **kwargs):  # noqa: ANN001
        notified.append(kwargs)
        return None

    import app.services.telegram_notify as tg

    original = tg.enqueue_file_changes_notification
    tg.enqueue_file_changes_notification = fake_enqueue  # type: ignore[assignment]
    try:
        # monkeypatch via module — проще через notify path
        service._maybe_notify_telegram(  # type: ignore[attr-defined]
            release_id=1,
            torrent_id=2,
            events=[
                SimpleNamespace(kind=KIND_ADDED, relative_path="a.mkv", full_path=None),
                SimpleNamespace(kind=KIND_MISSING, relative_path="b.mkv", full_path=None),
            ],
            archive=None,
            baseline=True,
        )
    finally:
        tg.enqueue_file_changes_notification = original  # type: ignore[assignment]

    assert len(notified) == 1
    kinds = [getattr(e, "kind", None) for e in notified[0]["events"]]
    assert kinds == [KIND_ADDED]


def test_early_sync_does_not_persist_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """master_added notify=False → events не пишем (TG на hash_torrent)."""
    from app.services.file_tracker import KIND_ADDED, TrackTorrentResult

    info_hash = "ab" * 20
    persisted: list = []
    db = MagicMock()
    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )

    def fake_sync(**kwargs):  # noqa: ANN003
        assert kwargs.get("persist_events") is False
        return SimpleNamespace(
            result=TrackTorrentResult(
                files_upserted=1,
                changes=[FileChange(kind=KIND_ADDED, relative_path="ep.mkv")],
            ),
            events=[],
            has_prior_version=False,
            baseline_had_known=False,
            save_path=None,
            content_path=None,
            first_seen_paths={"ep.mkv"},
        )

    monkeypatch.setattr(service, "_sync_composition", fake_sync)
    monkeypatch.setattr(
        service,
        "_persist_events",
        lambda **_k: persisted.append(_k) or [],
    )
    notified: list = []
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **kw: notified.append(kw))

    result = service.sync_torrent_composition(
        info_hash=info_hash, torrent_id=1, release_id=2, notify=False
    )
    assert result.files_upserted == 1
    assert persisted == []
    assert notified == []


def test_find_orphans_skips_ds_store(tmp_path: Path) -> None:
    from app.services.file_tracker import FileTrackerService, KIND_ORPHAN

    media = tmp_path / "anilibria"
    show = media / "Show"
    show.mkdir(parents=True)
    ep = show / "ep.mkv"
    ep.write_bytes(b"ok")
    ds = show / ".DS_Store"
    ds.write_bytes(b"junk")
    stray = show / "extra.mkv"
    stray.write_bytes(b"x")

    service = FileTrackerService(MagicMock())
    changes = service._find_orphans_under_root(
        root=show,
        known_full_paths={str(ep)},
        media_root=media,
    )
    paths = {c.full_path for c in changes if c.kind == KIND_ORPHAN}
    assert str(stray.resolve()) in paths
    assert not any(p and p.endswith(".DS_Store") for p in paths)


def test_track_clean_baseline_after_early_ok_settles_all_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После early ok без хэша hash_torrent оставляет/чинит все в new (добавления)."""
    from app.services.file_tracker import TrackTorrentResult, UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "ht" * 20
    rows: list[SimpleNamespace] = []
    for idx in range(1, 5):
        rel = f"Show/ep0{idx}.mkv"
        full = media / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"data")
        rows.append(
            SimpleNamespace(
                relative_path=rel,
                selected=True,
                full_path=str(full),
                ui_status=UI_STATUS_OK if idx == 1 else UI_STATUS_NEW,
            )
        )

    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    db.scalar.return_value = None

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[
                SimpleNamespace(kind="added", relative_path=r.relative_path, id=i)
                for i, r in enumerate(rows, start=1)
            ],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={r.relative_path for r in rows},
            baseline_had_known=False,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda paths, *_a, **_k: {"hashed": len(paths), "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    notified: list = []
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **kw: notified.append(kw))

    db.scalar.side_effect = lambda *a, **k: SimpleNamespace(content_hash="abc")

    # Sync mocked: статусы как после clean baseline sync (все new)
    for r in rows:
        r.ui_status = UI_STATUS_NEW

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=True)
    assert all(r.ui_status == UI_STATUS_NEW for r in rows)
    assert notified and notified[0].get("baseline") is True


def test_unnotified_events_filters_by_created_at_window() -> None:
    """Догон TG только для событий новее _UNNOTIFIED_EVENT_WINDOW."""
    from datetime import timedelta
    from unittest.mock import MagicMock

    from app.services.file_tracker import FileTrackerService, _UNNOTIFIED_EVENT_WINDOW

    assert _UNNOTIFIED_EVENT_WINDOW == timedelta(days=30)
    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    FileTrackerService(db)._unnotified_events(
        torrent_id=1, info_hash="ab" * 20, kinds={"added"}
    )
    stmt = str(db.scalars.call_args[0][0])
    assert "notified_at IS NULL" in stmt
    assert "created_at >=" in stmt


def test_track_notifies_unnotified_added_as_incremental(tmp_path, monkeypatch) -> None:
    """Mixed baseline: added уже в БД без notified_at → TG «изменения», не сводка."""
    from app.services.file_tracker import KIND_ADDED, TrackTorrentResult, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "cd" * 20
    rel = "Show/ep03.mkv"
    full = media / rel
    full.parent.mkdir(parents=True)
    full.write_bytes(b"data")
    file_row = SimpleNamespace(
        relative_path=rel, selected=True, full_path=str(full), ui_status=UI_STATUS_OK
    )
    pending_event = SimpleNamespace(
        id=99, kind=KIND_ADDED, relative_path=rel, full_path=str(full), notified_at=None
    )

    db = MagicMock()
    db.scalars.return_value.all.return_value = [file_row]
    db.scalar.return_value = SimpleNamespace(content_hash="h")

    service = FileTrackerService(db)
    monkeypatch.setattr(
        service,
        "_prepare_track",
        lambda **_k: SimpleNamespace(
            normalized_hash=info_hash,
            torrent_bytes=b"x",
            archive=None,
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(service, "_prior_version_hash", lambda **_k: None)
    monkeypatch.setattr(service, "_prior_version_hashes", lambda **_k: {})
    monkeypatch.setattr(service, "_unnotified_events", lambda **_k: [pending_event])
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[],  # early sync уже «съел» added
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rel},
            baseline_had_known=True,
        ),
    )
    monkeypatch.setattr("app.services.file_tracker.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.file_tracker.hash_paths_parallel",
        lambda *_a, **_k: {"hashed": 1, "gated": 0, "errors": 0, "stopped": False},
    )
    monkeypatch.setattr(service, "_filter_duplicate_changes", lambda **_k: [])
    monkeypatch.setattr(service, "_persist_events", lambda **_k: [])
    monkeypatch.setattr("app.services.file_tracker.resolve_orphan_scan_root", lambda **_k: None)
    notified: list = []
    monkeypatch.setattr(service, "_maybe_notify_telegram", lambda **kw: notified.append(kw))

    service.track_torrent(info_hash=info_hash, torrent_id=1, release_id=2, notify=True)
    assert len(notified) == 1
    assert notified[0]["baseline"] is False
    assert notified[0]["events"] == [pending_event]


def test_file_status_for_ui_checking_requires_prior_hash(tmp_path: Path) -> None:
    from app.services.file_tracker import file_status_for_ui

    present = tmp_path / "ep.mkv"
    present.write_bytes(b"data")
    disk_hash = SimpleNamespace(content_hash="abc123", size=1, mtime=1.0)
    assert (
        file_status_for_ui(
            relative_path="ep.mkv",
            full_path=str(present),
            ui_status="ok",
            disk_hash=disk_hash,
            hash_job_active=False,
            incomplete=False,
        )
        == "ok"
    )
    assert (
        file_status_for_ui(
            relative_path="ep.mkv",
            full_path=str(present),
            ui_status="new",
            disk_hash=None,
            hash_job_active=True,
            incomplete=False,
        )
        == "new"
    )
    assert (
        file_status_for_ui(
            relative_path="ep.mkv",
            full_path=str(present),
            ui_status="new",
            disk_hash=disk_hash,
            hash_job_active=True,
        )
        == "checking"
    )


def test_resolve_orphan_scan_root_prefers_content_path(tmp_path: Path) -> None:
    from app.services.file_tracker import resolve_orphan_scan_root

    media = tmp_path / "anilibria"
    year = media / "2012"
    show = year / "Sakurasou"
    show.mkdir(parents=True)
    (show / "ep01.mkv").write_bytes(b"1")
    other = year / "Nekomonogatari"
    other.mkdir()
    (other / "bonus.mkv").write_bytes(b"x")

    root = resolve_orphan_scan_root(
        save_path=str(year),
        content_path=str(show),
        known_full_paths={str(show / "ep01.mkv")},
        media_root=media,
    )
    assert root == show.resolve()


def test_resolve_orphan_scan_root_rejects_shared_year_save_path(tmp_path: Path) -> None:
    """save_path=год без content_path → не сканируем весь год."""
    from app.services.file_tracker import resolve_orphan_scan_root

    media = tmp_path / "anilibria"
    year = media / "2012"
    show = year / "Sakurasou"
    show.mkdir(parents=True)
    ep = show / "ep01.mkv"
    ep.write_bytes(b"1")

    # Без content_path корень из known = Show — ок (уже не год).
    root = resolve_orphan_scan_root(
        save_path=str(year),
        content_path=None,
        known_full_paths={str(ep)},
        media_root=media,
    )
    assert root == show.resolve()

    # Файл лежит прямо в годе (= save_path) — orphan-скан запрещён.
    flat = year / "movie.mkv"
    flat.write_bytes(b"m")
    assert (
        resolve_orphan_scan_root(
            save_path=str(year),
            content_path=str(flat),
            known_full_paths={str(flat)},
            media_root=media,
        )
        is None
    )
    assert (
        resolve_orphan_scan_root(
            save_path=str(year),
            content_path=None,
            known_full_paths={str(flat)},
            media_root=media,
        )
        is None
    )


def test_find_orphans_only_under_torrent_content(tmp_path: Path) -> None:
    from app.services.file_tracker import FileTrackerService, KIND_ORPHAN

    media = tmp_path / "anilibria"
    year = media / "2012"
    show = year / "Sakurasou"
    show.mkdir(parents=True)
    known = show / "ep01.mkv"
    known.write_bytes(b"ok")
    local_orphan = show / "extra.mkv"
    local_orphan.write_bytes(b"orphan")
    foreign = year / "Nekomonogatari" / "bonus.mkv"
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign")

    svc = FileTrackerService(MagicMock())
    changes = svc._find_orphans_under_root(
        root=show,
        known_full_paths={str(known.resolve())},
        media_root=media,
    )
    paths = {c.full_path for c in changes}
    assert str(local_orphan.resolve()) in paths
    assert str(foreign.resolve()) not in paths
    assert all(c.kind == KIND_ORPHAN for c in changes)
