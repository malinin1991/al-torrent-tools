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


def test_process_completion_retry_does_not_enqueue_hash_again() -> None:
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
    enqueue.assert_not_called()


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
