"""Тесты file tracking: парсер .torrent, gate BLAKE3, api_present, enqueue, TG, UI."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
        status=TorrentPipelineService.STATUS_DONE,
        error=None,
    )
    add_called = {"ok": False}

    def fake_add(p, _bytes):
        add_called["ok"] = True
        p.status = TorrentPipelineService.STATUS_DONE
        return done

    service._add_to_slave = MagicMock(side_effect=fake_add)  # type: ignore[method-assign]
    enqueue = MagicMock()
    service._enqueue_hash_torrent = enqueue  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent")

    assert result.status == TorrentPipelineService.STATUS_DONE
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
        status=TorrentPipelineService.STATUS_DONE,
        error=None,
    )
    service._add_to_slave = MagicMock(return_value=done)  # type: ignore[method-assign]
    enqueue = MagicMock()
    service._enqueue_hash_torrent = enqueue  # type: ignore[method-assign]

    result = service.process_completion(pipeline, b"torrent")

    assert result.status == TorrentPipelineService.STATUS_DONE
    service._add_to_slave.assert_called_once()
    enqueue.assert_called_once_with(done)


def test_enqueue_hash_skips_when_already_success() -> None:
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
    service._hash_torrent_already_done_or_queued = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._add_log = MagicMock()  # type: ignore[method-assign]

    service._enqueue_hash_torrent(pipeline)

    service._hash_torrent_already_done_or_queued.assert_called_once_with("abc123")


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
    service._add_log.assert_called()
    assert any("не удалось поставить" in str(c) for c in service._add_log.call_args_list)
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


def test_sync_composition_baseline_complete_on_disk_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: файл уже complete на диске → ok, не «новый»; missing → new."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_NEW, UI_STATUS_OK

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
    assert rows[ep01].ui_status == UI_STATUS_OK
    assert rows[ep02].ui_status == UI_STATUS_NEW
    assert synced.first_seen_paths == {ep02}


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
    monkeypatch.setattr(service, "_prior_version_paths", lambda **_k: {ep01})
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



def test_sync_composition_heals_added_when_rows_exist_without_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory успел записать torrent_files без events → heal added; baseline ok не трогаем."""
    from app.services.file_tracker import KIND_ADDED

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "cd" * 20
    rel = str(Path("Show Name") / "ep01.mkv")
    existing = SimpleNamespace(
        relative_path=rel,
        torrent_id=1,
        release_id=1,
        size=1,
        file_index=0,
        selected=True,
        full_path=str(media / rel),
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
        lambda *_a, **_k: media / "x.mkv",
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
    # Baseline: не апгрейдим ok → new при повторном sync.
    assert existing.ui_status == "ok"


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


def test_settle_ui_status_rules() -> None:
    from app.services.file_tracker import (
        FileTrackerService,
        UI_STATUS_CHANGED,
        UI_STATUS_NEW,
        UI_STATUS_OK,
    )

    # new/changed — финальные относительно prior, не понижаются
    row = SimpleNamespace(ui_status=UI_STATUS_NEW)
    FileTrackerService._settle_ui_status(row, first_seen=False, mismatch=True, matched=False)
    assert row.ui_status == UI_STATUS_NEW

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

    # baseline: early «новый» уходит в ok (чистый baseline)
    row.ui_status = UI_STATUS_NEW
    FileTrackerService._settle_ui_status(
        row, first_seen=True, mismatch=False, matched=False, is_baseline=True
    )
    assert row.ui_status == UI_STATUS_OK

    # baseline mixed: среди известных файлов настоящий new сохраняем
    row.ui_status = UI_STATUS_NEW
    FileTrackerService._settle_ui_status(
        row,
        first_seen=True,
        mismatch=False,
        matched=False,
        is_baseline=True,
        preserve_baseline_new=True,
    )
    assert row.ui_status == UI_STATUS_NEW

    # baseline + mismatch с уже известным disk hash → changed (граница кусков)
    row.ui_status = UI_STATUS_OK
    FileTrackerService._settle_ui_status(
        row, first_seen=False, mismatch=True, matched=False, is_baseline=True
    )
    assert row.ui_status == UI_STATUS_CHANGED


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
    monkeypatch.setattr(service, "_prior_version_paths", lambda **_k: {ep01})
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
    db.scalar.side_effect = [prev_hash, SimpleNamespace(content_hash=new_hash)]

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
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=list(events),
            has_prior_version=has_prior,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths={rel} if first_seen else set(),
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


def test_track_first_ever_file_settles_to_ok(tmp_path, monkeypatch) -> None:
    """Первый торрент релиза: early «новый» после hash-settle → ok."""
    from app.services.file_tracker import UI_STATUS_OK

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
    assert row.ui_status == UI_STATUS_OK
    assert len(notified) == 1
    assert notified[0]["baseline"] is True


def test_track_baseline_incomplete_not_forced_to_new(tmp_path, monkeypatch) -> None:
    """Baseline settle: .!qB не форсим в new — оставляем provisional (ok если был хэш)."""
    from app.services.file_tracker import TrackTorrentResult, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    info_hash = "55" * 20
    rel = "Show/ep01.mkv"
    full = media / rel
    full.parent.mkdir(parents=True)
    Path(str(full) + ".!qB").write_bytes(b"partial")

    file_row = SimpleNamespace(
        relative_path=rel, selected=True, full_path=str(full), ui_status="ok"
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
    monkeypatch.setattr(
        service,
        "_sync_composition",
        lambda **_k: SimpleNamespace(
            result=TrackTorrentResult(),
            events=[SimpleNamespace(kind="added", relative_path=rel)],
            has_prior_version=False,
            save_path=str(media),
            content_path=str(media),
            first_seen_paths=set(),
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
    assert file_row.ui_status == UI_STATUS_OK


def test_baseline_provisional_incomplete_with_and_without_hash(tmp_path: Path) -> None:
    """!qB: есть хэш без суффикса → ok; нет хэша → new."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

    media = tmp_path / "anilibria"
    media.mkdir()
    full = media / "Show" / "ep.mkv"
    full.parent.mkdir(parents=True)
    Path(str(full) + ".!qB").write_bytes(b"partial")

    service = FileTrackerService(MagicMock())
    assert (
        service._baseline_provisional_status(str(full), hashed_paths={str(full)})
        == UI_STATUS_OK
    )
    assert (
        service._baseline_provisional_status(str(full), hashed_paths=set())
        == UI_STATUS_NEW
    )
    # canonical path stored without suffix even if full_path points at .!qB file
    qb_path = str(full) + ".!qB"
    assert (
        service._baseline_provisional_status(qb_path, hashed_paths={str(full)})
        == UI_STATUS_OK
    )


def test_sync_composition_heals_baseline_new_when_hash_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stuck baseline new + content hash уже есть → ok; heal одним IN по full_paths."""
    from app.services.file_tracker import UI_STATUS_NEW, UI_STATUS_OK

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
            return FakeScalars([row_a, row_b])  # previous torrent_files
        if n == 2:
            return FakeScalars([])  # prior archive candidates — baseline
        # hashed canonical paths (provisional +/или heal)
        return FakeScalars([full_a, full_b])

    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.scalar.side_effect = [None, None]  # _has_prior_event ×2

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

    synced = service._sync_composition(  # type: ignore[attr-defined]
        normalized_hash=info_hash,
        torrent_id=1,
        release_id=2,
        torrent_bytes=b"x",
    )
    assert synced.has_prior_version is False
    assert row_a.ui_status == UI_STATUS_OK
    assert row_b.ui_status == UI_STATUS_OK
    # previous + prior candidates + hashed lookup (provisional закрывает heal)
    assert len(scalars_calls) == 3


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

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            # newest first: empty composition, then one with files
            return FakeScalars(["cc" * 20, "BB" + "b" * 38])
        if calls["n"] == 2:
            return FakeScalars(["BB" + "b" * 38])  # только у этого есть torrent_files
        return FakeScalars([])

    db.scalars.side_effect = fake_scalars
    service = FileTrackerService(db)
    prior = service._prior_version_hash(torrent_id=7, current_hash="aa" * 20)  # type: ignore[attr-defined]
    assert prior == ("bb" + "b" * 38)

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
    """Прошлая версия есть (архив), но её состав потерян → файлы НЕ помечаются «новый»."""
    from app.services.file_tracker import TorrentFile, UI_STATUS_OK

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
    monkeypatch.setattr(service, "_prior_version_paths", lambda **_k: set())
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
    # Состав неизвестен → файл считается существовавшим (ok), не «новый», без added-события.
    assert synced.first_seen_paths == set()
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert rows[0].ui_status == UI_STATUS_OK
    assert persisted == []


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
