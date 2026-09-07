"""Автотесты meta_sync / refresh_release_meta / rename / tag reconcile."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs import meta_sync as meta_sync_mod
from app.services import qbittorrent as qb_mod
from app.services.torrent_archive import TorrentArchiveService
from app.services.torrent_processor import TorrentProcessor


def test_update_archive_meta_from_api_payload_updates_description() -> None:
    db = MagicMock()
    archive = SimpleNamespace(
        torrent_id=7,
        release_id=1,
        info_hash="a" * 40,
        torrent_description="1-15",
        torrent_type="WEBRip 1080p HEVC",
        anime_name="Old",
        category="AniLibria/2024",
        description=None,
        release_alias="show",
        quality_json={"genres": ["Комедия"], "type": {"value": "WEBRip"}},
        api_created_at=None,
        api_present=True,
    )
    db.scalar.return_value = archive
    svc = TorrentArchiveService(db)

    status = svc.update_archive_meta_from_api_payload(
        release_id=1,
        torrent_payload={
            "id": 7,
            "info_hash": "a" * 40,
            "description": "1-16",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "HEVC"},
        },
        release_payload={"name": {"main": "Show", "english": "Show EN"}, "year": 2024},
        release_alias="show",
    )

    assert status == "updated"
    assert archive.torrent_description == "1-16"
    assert archive.anime_name == "Show"
    assert archive.quality_json["genres"] == ["Комедия"]
    db.commit.assert_called()


def test_update_archive_meta_hash_mismatch() -> None:
    db = MagicMock()
    archive = SimpleNamespace(
        torrent_id=7,
        release_id=1,
        info_hash="a" * 40,
        torrent_description="1-15",
        torrent_type=None,
        anime_name=None,
        category=None,
        description=None,
        release_alias=None,
        quality_json={},
        api_created_at=None,
        api_present=True,
    )
    db.scalar.return_value = archive
    svc = TorrentArchiveService(db)

    status = svc.update_archive_meta_from_api_payload(
        release_id=1,
        torrent_payload={"id": 7, "info_hash": "b" * 40, "description": "1-16"},
    )
    assert status == "hash_mismatch"
    assert archive.torrent_description == "1-15"
    db.commit.assert_not_called()


def test_ensure_torrent_rename_noop_when_name_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(qb_mod, "_read_torrent_name", lambda *_: "Show (1-16) [WEBRip]")
    rename = MagicMock()
    monkeypatch.setattr(qb_mod, "_apply_torrent_rename", rename)

    changed = qb_mod.ensure_torrent_rename(
        client, "a" * 40, "Show (1-16) [WEBRip]", require_present=True
    )
    assert changed is False
    rename.assert_not_called()


def test_ensure_torrent_rename_calls_when_different(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.torrents_info.return_value = [MagicMock(name="old")]
    monkeypatch.setattr(qb_mod, "_read_torrent_name", lambda *_: "Show (1-15) [WEBRip]")
    rename = MagicMock()
    monkeypatch.setattr(qb_mod, "_apply_torrent_rename", rename)

    changed = qb_mod.ensure_torrent_rename(
        client, "a" * 40, "Show (1-16) [WEBRip]", require_present=True
    )
    assert changed is True
    rename.assert_called_once()


def test_ensure_torrent_tags_reconcile_removes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(qb_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        qb_mod,
        "_read_torrent_tag_list",
        MagicMock(side_effect=[["Комедия", "Старый"], ["Комедия", "Драма"]]),
    )
    monkeypatch.setattr(
        qb_mod,
        "_read_torrent_tags",
        MagicMock(side_effect=[{"комедия", "старый"}, {"комедия", "драма"}]),
    )

    ok = qb_mod._ensure_torrent_tags(client, "a" * 40, ["Комедия", "Драма"])

    assert ok is True
    client.torrents_add_tags.assert_called_once()
    assert client.torrents_add_tags.call_args.kwargs["tags"] == ["Драма"]
    client.torrents_remove_tags.assert_called_once()
    assert client.torrents_remove_tags.call_args.kwargs["tags"] == ["Старый"]


def test_full_sync_refresh_renames_and_updates_archive(monkeypatch) -> None:
    """all_seen + refresh_qb_meta: archive 1-15→1-16, rename на master+slave, без TG."""
    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    torrent = {
        "id": 5,
        "info_hash": "b" * 40,
        "description": "1-16",
        "type": {"value": "WEBRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "HEVC"},
    }
    al.get_torrents_for_release = AsyncMock(return_value=[torrent])
    al.get_release = AsyncMock(
        return_value={
            "id": 20,
            "alias": "seen-show",
            "name": {"main": "Шоу", "english": "Show"},
            "genres": [{"name": "Драма"}],
        }
    )

    processor = TorrentProcessor(db=db, job_id=2, client=al)
    db.scalar.return_value = SimpleNamespace(info_hash="b" * 40)
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
    monkeypatch.setattr(
        "app.services.torrent_processor.update_api_present_for_release",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(processor, "_sync_hevc_pair_events", lambda *_: None)
    monkeypatch.setattr(processor, "_archive_hash_mismatches", lambda *_: False)

    archive_svc = MagicMock()
    archive_svc.fill_missing_api_created_at.return_value = 0
    archive_svc.update_archive_meta_from_api_payload.return_value = "updated"
    monkeypatch.setattr(
        "app.services.torrent_processor.TorrentArchiveService",
        lambda *_a, **_k: archive_svc,
    )

    monkeypatch.setattr(processor, "_persist_release_ui_meta_from_payload", MagicMock())
    monkeypatch.setattr(
        processor, "_resolve_genres_for_meta", AsyncMock(return_value=["Драма"])
    )
    refresh_comments = MagicMock(return_value=1)
    refresh_tags = MagicMock(return_value=1)
    refresh_renames = MagicMock(return_value=2)
    monkeypatch.setattr(processor, "_refresh_qb_comments", refresh_comments)
    monkeypatch.setattr(processor, "_refresh_qb_tags", refresh_tags)
    monkeypatch.setattr(processor, "_refresh_qb_renames", refresh_renames)
    tg = MagicMock()
    monkeypatch.setattr(
        "app.services.torrent_processor.enqueue_pipeline_telegram_notification", tg
    )

    stats = asyncio.run(processor.process_release(20, "seen-show", refresh_qb_meta=True))

    assert stats["renames"] == 2
    assert stats["comments"] == 1
    assert stats["tags"] == 1
    # updated — только торренты (force/Conflict), не сумма comments+tags+renames
    assert stats["updated"] == 0
    archive_svc.update_archive_meta_from_api_payload.assert_called()
    assert (
        archive_svc.update_archive_meta_from_api_payload.call_args.kwargs["torrent_payload"][
            "description"
        ]
        == "1-16"
    )
    refresh_renames.assert_called_once()
    tg.assert_not_called()


def test_meta_sync_requires_scope() -> None:
    with pytest.raises(ValueError, match="release_id"):
        asyncio.run(meta_sync_mod.run_meta_sync(MagicMock(), job_id=1, params={}))


def test_meta_sync_scoped_release_calls_process(monkeypatch) -> None:
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

        async def process_release(self, **kwargs):
            called["kwargs"] = kwargs
            return {**TorrentProcessor.empty_release_stats(), "renames": 1}

    monkeypatch.setattr(meta_sync_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(meta_sync_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(meta_sync_mod, "build_anilibria_client", lambda _db: MagicMock())

    asyncio.run(
        meta_sync_mod.run_meta_sync(MagicMock(), job_id=1, params={"release_id": 42})
    )

    assert called["kwargs"]["release_id"] == 42
    assert called["kwargs"]["refresh_qb_meta"] is True


def test_meta_sync_torrent_scope_handoff_unseen(monkeypatch) -> None:
    """Unseen torrent_id → process_release, не clear seen."""
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)
        RELEASE_TORRENTS_INCLUDE = TorrentProcessor.RELEASE_TORRENTS_INCLUDE
        _iter_torrents = staticmethod(TorrentProcessor._iter_torrents)
        _to_int = staticmethod(TorrentProcessor._to_int)

        def __init__(self, **kwargs):
            self._db = MagicMock()
            self._db.scalar.return_value = None
            self._al_client = MagicMock()
            self._al_client.get_torrents_for_release = AsyncMock(
                return_value=[{"id": 9, "info_hash": "c" * 40}]
            )

        def _add_log(self, *a, **k):
            return None

        def _normalize_api_info_hash(self, raw):
            return TorrentProcessor._normalize_api_info_hash(raw)

        def build_seen_exists_query(self, torrent_id, info_hash):
            return MagicMock()

        def _archive_hash_mismatches(self, torrent):
            return False

        async def process_release(self, **kwargs):
            called["process"] = kwargs
            return TorrentProcessor.empty_release_stats()

        async def refresh_release_meta(self, *a, **k):
            called["meta"] = True
            return {"comments": 0, "tags": 0, "renames": 0, "archive": 0, "handoff": 0}

    monkeypatch.setattr(meta_sync_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(meta_sync_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(meta_sync_mod, "build_anilibria_client", lambda _db: MagicMock())

    asyncio.run(
        meta_sync_mod.run_meta_sync(
            MagicMock(), job_id=1, params={"release_id": 3, "torrent_id": 9}
        )
    )

    assert "process" in called
    assert called["process"]["refresh_qb_meta"] is True
    assert "meta" not in called


def test_full_meta_sync_job_uses_refresh_no_backfill(monkeypatch) -> None:
    called: dict = {"backfill": 0}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        merge_release_stats = classmethod(
            lambda cls, acc, part: TorrentProcessor.merge_release_stats(acc, part)
        )
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

        async def process_release(self, **kwargs):
            called["kwargs"] = kwargs
            return TorrentProcessor.empty_release_stats()

        def backfill_qb_comments_from_archive(self):
            called["backfill"] += 1
            return {"archives": 0, "updated": 0, "missing": 0, "tags_updated": 0}

    monkeypatch.setattr(meta_sync_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(meta_sync_mod, "_setting_int", lambda *a, **k: 0)
    monkeypatch.setattr(meta_sync_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(meta_sync_mod, "_extract_list", lambda *_: [{"id": 7, "alias": "y"}])
    monkeypatch.setattr(meta_sync_mod, "_extract_total_pages", lambda *_: 1)
    monkeypatch.setattr(meta_sync_mod, "normalize_api_datetime", lambda v: v)
    al = MagicMock()
    al.catalog_releases = AsyncMock(return_value={"list": [{"id": 7}]})
    monkeypatch.setattr(meta_sync_mod, "build_anilibria_client", lambda _db: al)

    asyncio.run(meta_sync_mod.run_full_meta_sync(MagicMock(), job_id=1, params={}))

    assert called["kwargs"].get("refresh_qb_meta") is True
    assert called["backfill"] == 0


def test_job_catalog_includes_meta_sync_types() -> None:
    from app.services.job_catalog import JOB_TYPE_DEFS

    types = {item.type for item in JOB_TYPE_DEFS}
    assert "meta_sync" in types
    assert "full_meta_sync" in types
    assert "force_release_sync" in types
    meta = next(item for item in JOB_TYPE_DEFS if item.type == "meta_sync")
    assert meta.manual_run is True
    assert meta.interval_default_sec is None
    force = next(item for item in JOB_TYPE_DEFS if item.type == "force_release_sync")
    assert force.manual_run is True
    assert force.interval_default_sec is None
