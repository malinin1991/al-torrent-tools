"""full_sync force_qb_load: повторная загрузка .torrent на master и slave для all_seen."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.jobs import full_sync as full_sync_mod
from app.services.torrent_processor import TorrentProcessor


def test_full_sync_job_passes_force_qb_load(monkeypatch) -> None:
    called: dict = {}

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

    asyncio.run(full_sync_mod.run_full_sync(MagicMock(), job_id=1, params={"force_qb_load": True}))

    assert called["kwargs"]["refresh_qb_meta"] is True
    assert called["kwargs"]["force_qb_load"] is True
    assert called["kwargs"]["release_id"] == 7


def test_process_release_all_seen_force_qb_load_adds_to_both(monkeypatch, tmp_path: Path) -> None:
    info_hash = "a" * 40
    torrent_bytes = b"d4:infod4:name4:testee"
    torrent_file = tmp_path / f"{info_hash}.torrent"
    torrent_file.write_bytes(torrent_bytes)

    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    al.passkey = "pk"
    al.get_torrents_for_release = AsyncMock(
        return_value=[{"id": 5, "info_hash": info_hash}]
    )
    al.get_release = AsyncMock(
        return_value={
            "id": 20,
            "alias": "seen-show",
            "year": 2026,
            "genres": [{"name": "Драма"}],
        }
    )
    al.download_torrent_file = AsyncMock(side_effect=AssertionError("не должны качать"))

    processor = TorrentProcessor(db=db, job_id=2, client=al)
    db.scalar.return_value = SimpleNamespace(info_hash=info_hash)
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
    monkeypatch.setattr(
        "app.services.torrent_processor.ensure_passkey_stored",
        AsyncMock(return_value="pk"),
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.ensure_announce_passkey",
        lambda data, _pk: data,
    )

    archive = SimpleNamespace(
        info_hash=info_hash,
        torrent_id=5,
        file_path=str(torrent_file),
        category="AniLibria/2026",
        quality_json={"genres": ["Драма"]},
        release_alias="seen-show",
        anime_name="Seen Show",
        torrent_description="WEBRip 1080p",
        torrent_type="webrip",
    )
    archive_svc = SimpleNamespace(
        fill_missing_api_created_at=lambda *a, **k: 0,
        update_archive_meta_from_api_payload=lambda *a, **k: "noop",
        _find_active_archive=lambda *a, **k: archive,
        resolve_file_path=lambda _a: torrent_file,
        _build_category=lambda *_: "AniLibria/2026",
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.TorrentArchiveService",
        lambda *_a, **_k: archive_svc,
    )
    monkeypatch.setattr(processor, "_refresh_qb_comments", MagicMock(return_value=0))
    monkeypatch.setattr(processor, "_refresh_qb_tags", MagicMock(return_value=0))
    monkeypatch.setattr(processor, "_refresh_qb_renames", MagicMock(return_value=0))
    monkeypatch.setattr(processor, "_sync_hevc_pair_events", lambda *_: None)
    monkeypatch.setattr(processor, "_archive_hash_mismatches", lambda *_: False)
    monkeypatch.setattr(processor, "_persist_release_ui_meta_from_payload", lambda *a, **k: None)

    master = MagicMock(name="master")
    slave = MagicMock(name="slave")
    monkeypatch.setattr(
        processor,
        "_qb_clients_for_meta",
        lambda: [("master", master), ("slave", slave)],
    )

    add_calls: list[tuple[str, object]] = []

    def fake_add(client, data, **kwargs):
        role = "master" if client is master else "slave"
        add_calls.append((role, data))
        return (True, True, True)

    monkeypatch.setattr("app.services.torrent_processor.qb_add_torrent", fake_add)

    stats = asyncio.run(
        processor.process_release(20, "seen-show", refresh_qb_meta=True, force_qb_load=True)
    )

    assert stats["added"] == 2
    assert {role for role, _ in add_calls} == {"master", "slave"}
    assert all(data == torrent_bytes for _, data in add_calls)
    al.download_torrent_file.assert_not_called()
