"""Проверка, что genre tags проходят через ongoing и full_sync."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.jobs import full_sync as full_sync_mod
from app.jobs import ongoing as ongoing_mod
from app.services.torrent_processor import TorrentProcessor


def test_ongoing_passes_tags_on_new_torrent(monkeypatch) -> None:
    """Ongoing → process_release без refresh: qb_add_torrent получает genres."""
    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    al.passkey = "pk"
    al.get_torrents_for_release = AsyncMock(
        return_value=[{"id": 1, "info_hash": "a" * 40}]
    )
    al.get_release = AsyncMock(
        return_value={
            "id": 10,
            "alias": "test-show",
            "name": {"main": "Тест", "english": "Test"},
            "year": 2024,
            "genres": [{"id": 1, "name": "Комедия"}, {"id": 2, "name": "Романтика"}],
        }
    )
    al.download_torrent_file = AsyncMock(
        return_value=b"d8:announce14:http://tracker4:infod4:name8:test.bin6:lengthi1eee"
    )

    processor = TorrentProcessor(db=db, job_id=1, client=al)
    # not seen
    db.scalar.return_value = None
    monkeypatch.setattr(processor, "_try_connect_master", lambda: MagicMock())
    monkeypatch.setattr(processor, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(processor, "_mark_seen", lambda **k: None)
    monkeypatch.setattr(processor, "_persist_genres_to_archives", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.torrent_processor.ensure_passkey_stored",
        AsyncMock(return_value="pk"),
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.mark_release_processed",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.torrents_fingerprint",
        lambda *_: "fp",
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.should_skip_by_torrents_fingerprint",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.extract_release_markers",
        lambda *_: ("u", "f"),
    )
    archive_svc = MagicMock()
    archive_svc._build_category.return_value = "AniLibria/2024"
    archive_svc.save_torrent.return_value = MagicMock()
    monkeypatch.setattr(
        "app.services.torrent_processor.TorrentArchiveService",
        lambda *_a, **_k: archive_svc,
    )
    pipeline = MagicMock()
    pipeline.create_discovered.return_value = SimpleNamespace(status="discovered", id=1)
    pipeline.mark_master_added.return_value = None
    processor._pipeline = pipeline

    qb_add = MagicMock(return_value=(True, True, True))
    monkeypatch.setattr("app.services.torrent_processor.qb_add_torrent", qb_add)

    stats = asyncio.run(processor.process_release(10, "test-show"))

    assert stats["added"] == 1
    assert qb_add.call_args.kwargs["tags"] == ["Комедия", "Романтика"]


def test_full_sync_refresh_applies_tags_for_seen(monkeypatch) -> None:
    """Full sync refresh_qb_meta=True: для all_seen вызывается refresh tags."""
    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    al.get_torrents_for_release = AsyncMock(
        return_value=[{"id": 5, "info_hash": "b" * 40}]
    )
    al.get_release = AsyncMock(
        return_value={"id": 20, "alias": "seen-show", "genres": [{"name": "Драма"}]}
    )

    processor = TorrentProcessor(db=db, job_id=2, client=al)
    seen = SimpleNamespace(info_hash="b" * 40)
    db.scalar.return_value = seen
    monkeypatch.setattr(processor, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.torrent_processor.torrents_fingerprint",
        lambda *_: "fp",
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.should_skip_by_torrents_fingerprint",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.mark_release_processed",
        lambda *a, **k: None,
    )

    refresh_comments = MagicMock(return_value=1)
    refresh_tags = MagicMock(return_value=2)
    monkeypatch.setattr(processor, "_refresh_qb_comments", refresh_comments)
    monkeypatch.setattr(processor, "_refresh_qb_tags", refresh_tags)
    monkeypatch.setattr(
        processor,
        "_resolve_genres_for_meta",
        AsyncMock(return_value=["Драма"]),
    )

    stats = asyncio.run(
        processor.process_release(20, "seen-show", refresh_qb_meta=True)
    )

    assert stats["comments"] == 1
    assert stats["tags"] == 2
    assert stats["updated"] == 3
    refresh_comments.assert_called_once()
    refresh_tags.assert_called_once()
    assert refresh_tags.call_args.kwargs["genre_tags"] == ["Драма"]


def test_ongoing_job_calls_process_without_refresh(monkeypatch) -> None:
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        merge_release_stats = classmethod(lambda cls, acc, part: TorrentProcessor.merge_release_stats(acc, part))
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

        async def process_release(self, **kwargs):
            called["kwargs"] = kwargs
            return TorrentProcessor.empty_release_stats()

    monkeypatch.setattr(ongoing_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(ongoing_mod, "_setting_int", lambda *a, **k: 0)
    monkeypatch.setattr(ongoing_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(
        ongoing_mod,
        "_extract_releases_from_schedule",
        lambda *_: [
            ongoing_mod.ReleaseRef(release_id=1, alias="x", updated_at="u", fresh_at="f")
        ],
    )
    monkeypatch.setattr(ongoing_mod, "should_skip_unchanged", lambda *a, **k: False)

    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    al = MagicMock()
    al.get_schedule_week = AsyncMock(return_value=[])
    monkeypatch.setattr(ongoing_mod, "build_anilibria_client", lambda _db: al)

    asyncio.run(ongoing_mod.run_ongoing(db, job_id=1, params={}))

    assert called["kwargs"].get("refresh_qb_meta") in (None, False)
    assert called["kwargs"]["release_id"] == 1


def test_full_sync_job_calls_process_with_refresh(monkeypatch) -> None:
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        merge_release_stats = classmethod(lambda cls, acc, part: TorrentProcessor.merge_release_stats(acc, part))
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

        async def process_release(self, **kwargs):
            called["kwargs"] = kwargs
            return TorrentProcessor.empty_release_stats()

        def backfill_qb_comments_from_archive(self):
            return {"archives": 0, "updated": 0, "missing": 0, "tags_updated": 0}

    monkeypatch.setattr(full_sync_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(full_sync_mod, "_setting_int", lambda *a, **k: 0)
    monkeypatch.setattr(full_sync_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(full_sync_mod, "_extract_list", lambda *_: [{"id": 7, "alias": "y"}])
    monkeypatch.setattr(full_sync_mod, "_extract_total_pages", lambda *_: 1)
    monkeypatch.setattr(full_sync_mod, "normalize_api_datetime", lambda v: v)

    al = MagicMock()
    al.catalog_releases = AsyncMock(return_value={"list": [{"id": 7, "alias": "y"}]})
    monkeypatch.setattr(full_sync_mod, "build_anilibria_client", lambda _db: al)

    asyncio.run(full_sync_mod.run_full_sync(MagicMock(), job_id=1, params={}))

    assert called["kwargs"]["refresh_qb_meta"] is True
    assert called["kwargs"]["release_id"] == 7
