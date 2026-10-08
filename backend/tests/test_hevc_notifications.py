"""DB-регрессии: SLA без API sync и первый AVC после полного hash-прохода."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    PipelineEvent,
    TelegramBotAccess,
    TelegramOutbox,
    TorrentArchive,
    TorrentFile,
    TorrentPipeline,
)
from app.jobs import hash_torrent
from app.services.file_tracker import TrackTorrentResult
from app.services import hevc_notifications as notifications
from app.services.hevc_bot import HevcReleaseStatus
from app.services.hevc_pairing import sync_hevc_pair_events_for_release


@compiles(JSONB, "sqlite")
def _jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        notifications,
        "query_release_detail",
        lambda db, rid: HevcReleaseStatus(
            release_id=rid,
            alias=f"show-{rid}",
            title="Новое аниме",
            original_title=None,
            executors=[],
            items=[],
        ),
    )
    with Session(engine) as session:
        for bot, subject, tid, status in [
            ("hevc", "chat", -1001, "approved"),
            ("hevc", "chat", -1002, "pending"),
            ("hevc", "chat", -1003, "rejected"),
            ("hevc", "user", 42, "approved"),
            ("primary", "chat", -1004, "approved"),
        ]:
            session.add(
                TelegramBotAccess(
                    bot_key=bot,
                    subject_type=subject,
                    telegram_id=tid,
                    status=status,
                )
            )
        session.commit()
        yield session
    engine.dispose()


def archive(db, *, tid=10, rid=1, codec="AVC", episodes="1-2", created=None, **kwargs):
    row = TorrentArchive(
        info_hash=f"{tid:040x}",
        torrent_id=tid,
        release_id=rid,
        file_path=f"/test/{tid}.torrent",
        release_alias=f"show-{rid}",
        torrent_type=f"WEBRip 1080p {codec}",
        torrent_description=episodes,
        quality_json={
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": codec},
        },
        created_at=created or datetime(2026, 10, 8),
        api_created_at=created or datetime(2026, 10, 8),
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


def file(db, row, name="01.mkv", **kwargs):
    defaults = {"selected": True, "media_present": True, "is_checking": False}
    defaults.update(kwargs)
    item = TorrentFile(
        info_hash=row.info_hash,
        torrent_id=row.torrent_id,
        release_id=row.release_id,
        relative_path=name,
        full_path=f"/anilibria/{name}",
        ui_status="new",
        **defaults,
    )
    db.add(item)
    db.commit()
    return item


def announce(db, row, count=1):
    return notifications.enqueue_new_release_notification(
        db,
        release_id=row.release_id,
        info_hash=row.info_hash,
        hashed_files=count,
    )


def test_sla_passage_enqueues_once_without_api_changes(db):
    before = datetime(2026, 10, 8, 12)
    avc = archive(db, created=before - timedelta(hours=23))
    archive(db, tid=11, codec="HEVC", episodes="1", created=before)
    pipeline = TorrentPipeline(
        release_id=1,
        torrent_id=avc.torrent_id,
        info_hash=avc.info_hash,
        status="done",
    )
    db.add(pipeline)
    db.commit()
    sync_hevc_pair_events_for_release(db, 1, now=before)
    assert notifications.sync_overdue_notifications(db, now=before) == 0
    assert db.scalars(select(TelegramOutbox)).all() == []
    after = before + timedelta(hours=2)  # Меняются только часы, не API/архив.
    assert notifications.sync_overdue_notifications(db, now=after) == 1
    assert notifications.sync_overdue_notifications(db, now=after) == 0
    rows = db.scalars(select(TelegramOutbox)).all()
    assert len(rows) == 1
    assert rows[0].chat_id == "-1001"
    assert rows[0].bot_key == "hevc"
    assert rows[0].payload_json["kind"] == "hevc_overdue"
    assert rows[0].status == "pending"
    assert (
        len(
            db.scalars(
                select(PipelineEvent).where(PipelineEvent.to_status == "overdue")
            ).all()
        )
        == 1
    )


def test_periodic_scan_keeps_old_missing_and_ignore_semantics(db):
    old = datetime(2026, 9, 1)
    archive(db, created=old)
    avc = archive(db, tid=20, rid=2, created=old, ignore_hevc=True)
    archive(db, tid=21, rid=2, codec="HEVC", episodes="1", created=old)
    db.add(
        TorrentPipeline(
            release_id=2, torrent_id=20, info_hash=avc.info_hash, status="done"
        )
    )
    db.commit()
    assert (
        notifications.sync_overdue_notifications(db, now=old + timedelta(days=2)) == 0
    )
    assert db.scalars(select(TelegramOutbox)).all() == []


def test_periodic_failure_does_not_block_other_releases(db, monkeypatch):
    old = datetime(2026, 10, 1)
    for rid in (1, 2):
        archive(db, rid=rid, tid=rid * 10, created=old)
        archive(db, rid=rid, tid=rid * 10 + 1, codec="HEVC", episodes="1", created=old)
    checked = []

    def sync(_db, rid, **kwargs):
        checked.append(rid)
        if rid == 1:
            raise RuntimeError("temporary failure")
        return 1

    monkeypatch.setattr(notifications, "sync_hevc_pair_events_for_release", sync)
    assert (
        notifications.sync_overdue_notifications(db, now=old + timedelta(days=2)) == 1
    )
    assert checked == [1, 2]


def test_first_avc_announced_once_only_to_approved_hevc_groups(db):
    first = archive(db)
    file(db, first)
    assert announce(db, first) == 1
    rid, ih = first.release_id, first.info_hash
    db.close()
    assert (
        notifications.enqueue_new_release_notification(
            db, release_id=rid, info_hash=ih, hashed_files=1
        )
        == 0
    )
    rows = db.scalars(select(TelegramOutbox)).all()
    assert len(rows) == 1
    message = rows[0]
    assert message.bot_key == "hevc"
    assert message.chat_id == "-1001"
    assert message.pipeline_id is None
    assert message.payload_json["kind"] == "hevc_new_release"
    assert "Обнаружен новый релиз" in message.payload_json["text"]
    assert "хеширование файлов завершено" in message.payload_json["text"]
    assert (
        message.payload_json["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "status:1"
    )


@pytest.mark.parametrize("codec", ["HEVC", "AV1"])
def test_first_non_avc_is_not_announced(db, codec):
    row = archive(db, codec=codec)
    file(db, row)
    assert announce(db, row) == 0


@pytest.mark.parametrize("prior_codec", ["HEVC", "AVC"])
def test_existing_release_not_new_even_when_previous_torrent_removed(db, prior_codec):
    archive(db, codec=prior_codec, api_present=False, superseded=True)
    row = archive(db, tid=11)
    file(db, row)
    assert announce(db, row) == 0


@pytest.mark.parametrize(
    "state", ["no_files", "unselected", "checking", "missing", "partial"]
)
def test_no_announcement_until_all_selected_files_hashed(db, state):
    row = archive(db)
    if state != "no_files":
        file(
            db,
            row,
            selected=state != "unselected",
            is_checking=state == "checking",
            media_present=state != "missing",
        )
    if state == "partial":
        file(db, row, "02.mkv")
    assert announce(db, row) == 0
    assert db.scalars(select(TelegramOutbox)).all() == []


@pytest.mark.parametrize("cached", [False, True])
def test_hash_job_announces_after_tracking_and_supports_cached_hashes(
    db, monkeypatch, cached
):
    row = archive(db)
    monkeypatch.setattr(hash_torrent, "is_stop_requested", lambda *args: False)
    monkeypatch.setattr(hash_torrent, "_pipeline_for_hash", lambda *args: None)

    def track(**kwargs):
        assert db.scalars(select(TelegramOutbox)).all() == []
        file(db, row)
        return TrackTorrentResult(
            files_upserted=1, hashed=int(not cached), gated=int(cached)
        )

    tracker = MagicMock()
    tracker.track_torrent.side_effect = track
    monkeypatch.setattr(
        hash_torrent, "FileTrackerService", lambda *args, **kwargs: tracker
    )
    asyncio.run(
        hash_torrent.run_hash_torrent(
            db,
            1,
            {
                "release_id": row.release_id,
                "torrent_id": row.torrent_id,
                "info_hash": row.info_hash,
            },
        )
    )
    assert len(db.scalars(select(TelegramOutbox)).all()) == 1


@pytest.mark.parametrize(
    "result",
    [
        TrackTorrentResult(skipped_reason="торрент не api_present (архивный)"),
        TrackTorrentResult(files_upserted=1, errors=1, hashed=1),
        TrackTorrentResult(files_upserted=1),
    ],
)
def test_hash_job_does_not_announce_skip_errors_or_empty_hash_pass(
    db, monkeypatch, result
):
    row = archive(db)
    file(db, row)
    monkeypatch.setattr(hash_torrent, "is_stop_requested", lambda *args: False)
    monkeypatch.setattr(hash_torrent, "_pipeline_for_hash", lambda *args: None)
    tracker = MagicMock()
    tracker.track_torrent.return_value = result
    monkeypatch.setattr(
        hash_torrent, "FileTrackerService", lambda *args, **kwargs: tracker
    )
    asyncio.run(
        hash_torrent.run_hash_torrent(
            db,
            1,
            {
                "release_id": row.release_id,
                "torrent_id": row.torrent_id,
                "info_hash": row.info_hash,
            },
        )
    )
    assert db.scalars(select(TelegramOutbox)).all() == []


@pytest.mark.parametrize("offset, expected", [(-1, 0), (0, 1), (1, 1)])
def test_missing_sla_only_for_releases_added_since_october_first(db, offset, expected):
    created = notifications.MISSING_RELEASE_ADDED_SINCE + timedelta(seconds=offset)
    archive(db, created=created)
    assert (
        notifications.sync_overdue_notifications(
            db, now=created + timedelta(hours=23, minutes=59)
        )
        == 0
    )
    assert (
        notifications.sync_overdue_notifications(db, now=created + timedelta(hours=24))
        == expected
    )
    assert (
        notifications.sync_overdue_notifications(db, now=created + timedelta(days=3))
        == 0
    )
    rows = db.scalars(select(TelegramOutbox)).all()
    assert len(rows) == expected
    if expected:
        assert rows[0].payload_json["kind"] == "hevc_missing_sla"
        assert rows[0].chat_id == "-1001"
        assert rows[0].bot_key == "hevc"
        assert "HEVC не появился за 24 часа" in rows[0].payload_json["text"]
    # Новое уведомление не изменяет состояние pairing на overdue.
    assert db.scalars(select(PipelineEvent)).all() == []


def test_missing_new_avc_on_old_release_does_not_send(db):
    archive(db, created=datetime(2026, 9, 1), api_present=False, superseded=True)
    archive(db, tid=11, created=datetime(2026, 10, 2))
    assert notifications.sync_overdue_notifications(db, now=datetime(2026, 10, 4)) == 0
    assert db.scalars(select(TelegramOutbox)).all() == []


def test_missing_ignored_new_release_does_not_send(db):
    archive(db, created=datetime(2026, 10, 2), ignore_hevc=True)
    assert notifications.sync_overdue_notifications(db, now=datetime(2026, 10, 4)) == 0
    assert db.scalars(select(TelegramOutbox)).all() == []


def test_missing_resolved_before_deadline_does_not_send(db):
    created = datetime(2026, 10, 2)
    archive(db, created=created)
    assert (
        notifications.sync_overdue_notifications(db, now=created + timedelta(hours=23))
        == 0
    )
    archive(db, tid=11, codec="HEVC", created=created + timedelta(hours=23))
    assert (
        notifications.sync_overdue_notifications(db, now=created + timedelta(hours=25))
        == 0
    )
    assert db.scalars(select(TelegramOutbox)).all() == []
