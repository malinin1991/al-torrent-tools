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
    a = SimpleNamespace(torrent_id=1, api_present=True)
    b = SimpleNamespace(torrent_id=2, api_present=True)
    c = SimpleNamespace(torrent_id=3, api_present=False)
    db.scalars.return_value.all.return_value = [a, b, c]

    stats = update_api_present_for_release(db, release_id=10, present_torrent_ids={1, 3})

    assert a.api_present is True
    assert b.api_present is False
    assert c.api_present is True
    assert stats["false"] == 1
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


def test_filter_duplicate_changes_skips_repeated_missing() -> None:
    db = MagicMock()
    service = FileTrackerService(db)
    db.scalar.side_effect = [object(), None]

    filtered = service._filter_duplicate_changes(  # type: ignore[attr-defined]
        torrent_id=10,
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


def test_file_status_for_ui_disk_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.file_tracker import file_status_for_ui

    present = tmp_path / "present.mkv"
    present.write_bytes(b"x")
    missing = tmp_path / "missing.mkv"
    incomplete = tmp_path / "dl.mkv"
    (tmp_path / "dl.mkv.!qB").write_bytes(b"partial")

    assert (
        file_status_for_ui(relative_path="m.mkv", full_path=str(missing), in_torrent=True)
        == "new"
    )
    assert (
        file_status_for_ui(relative_path="p.mkv", full_path=str(present), in_torrent=True)
        == "ok"
    )
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path=str(present),
            latest_kind="modified",
            in_torrent=True,
        )
        == "changed"
    )
    assert (
        file_status_for_ui(
            relative_path="old.mkv",
            full_path=str(present),
            in_torrent=False,
        )
        == "removed"
    )
    # Новый .!qB без прежнего hash → new
    assert (
        file_status_for_ui(
            relative_path="dl.mkv",
            full_path=str(incomplete),
            disk_hash=None,
            in_torrent=True,
        )
        == "new"
    )
    # Известный файл ушёл в .!qB → checking
    assert (
        file_status_for_ui(
            relative_path="dl.mkv",
            full_path=str(incomplete),
            disk_hash=SimpleNamespace(content_hash="abc", size=1, mtime=1.0),
            in_torrent=True,
        )
        == "checking"
    )
    # Событие added не делает «новый», если файл уже на диске
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path=str(present),
            latest_kind="added",
            in_torrent=True,
        )
        == "ok"
    )
    # Active hash job на известном файле → checking
    assert (
        file_status_for_ui(
            relative_path="p.mkv",
            full_path=str(present),
            disk_hash=SimpleNamespace(content_hash="abc", size=1, mtime=1.0),
            hash_job_active=True,
            in_torrent=True,
        )
        == "checking"
    )


def test_file_status_for_ui_checking_requires_prior_hash(tmp_path: Path) -> None:
    from app.services.file_tracker import file_status_for_ui

    present = tmp_path / "ep.mkv"
    present.write_bytes(b"data")
    disk_hash = SimpleNamespace(content_hash="abc123", size=1, mtime=1.0)
    assert (
        file_status_for_ui(
            relative_path="ep.mkv",
            full_path=str(present),
            disk_hash=disk_hash,
            hash_job_active=False,
        )
        == "ok"
    )
    assert (
        file_status_for_ui(
            relative_path="ep.mkv",
            full_path=str(present),
            disk_hash=None,
            hash_job_active=True,
        )
        == "ok"
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
