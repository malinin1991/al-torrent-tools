"""full_sync all_seen: состав/блокировки пишутся даже если жанры уже в архиве."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.torrent_processor import TorrentProcessor


def test_resolve_genres_for_meta_fetches_even_when_archive_has_genres(monkeypatch) -> None:
    """Без payload не коротнуть по quality_json — иначе members не попадут в releases."""
    db = MagicMock()
    al = MagicMock()
    al.get_release = AsyncMock(
        return_value={
            "id": 7,
            "alias": "show",
            "genres": [{"name": "Экшен"}],
            "members": [
                {
                    "id": "u1",
                    "nickname": "Zvukar",
                    "role": {"value": "voicing", "description": "Озвучка"},
                }
            ],
            "is_blocked_by_geo": True,
            "is_blocked_by_copyrights": False,
        }
    )
    processor = TorrentProcessor(db=db, job_id=1, client=al)
    monkeypatch.setattr(processor, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(
        processor, "_genres_from_archives", lambda *_: ["СтарыйЖанр"]
    )
    persist = MagicMock()
    monkeypatch.setattr(processor, "_persist_release_ui_meta_from_payload", persist)

    genres = asyncio.run(processor._resolve_genres_for_meta(7))

    assert genres == ["Экшен"]
    al.get_release.assert_awaited_once()
    persist.assert_called_once()
    assert persist.call_args.args[0] == 7
    assert persist.call_args.args[1]["members"][0]["nickname"] == "Zvukar"


def test_full_sync_all_seen_calls_get_release_for_ui_meta(monkeypatch) -> None:
    """refresh_qb_meta + all_seen → get_release (не только жанры из архива)."""
    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    al.get_torrents_for_release = AsyncMock(
        return_value=[{"id": 5, "info_hash": "b" * 40}]
    )
    al.get_release = AsyncMock(
        return_value={
            "id": 20,
            "alias": "seen-show",
            "genres": [{"name": "Драма"}],
            "members": [
                {
                    "nickname": "Timer",
                    "role": {"value": "timing", "description": "Тайминг"},
                }
            ],
            "is_blocked_by_geo": False,
            "is_blocked_by_copyrights": True,
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
    monkeypatch.setattr(
        "app.services.torrent_processor.TorrentArchiveService",
        lambda *_a, **_k: SimpleNamespace(fill_missing_api_created_at=lambda *a, **k: 0),
    )
    monkeypatch.setattr(processor, "_refresh_qb_comments", MagicMock(return_value=0))
    monkeypatch.setattr(processor, "_refresh_qb_tags", MagicMock(return_value=0))
    monkeypatch.setattr(processor, "_sync_hevc_pair_events", lambda *_: None)
    # Жанры уже в архиве — раньше это блокировало get_release.
    monkeypatch.setattr(processor, "_genres_from_archives", lambda *_: ["Драма"])
    persist = MagicMock()
    monkeypatch.setattr(processor, "_persist_release_ui_meta_from_payload", persist)
    monkeypatch.setattr(processor, "_persist_genres_to_archives", lambda *a, **k: None)
    monkeypatch.setattr(
        processor, "_persist_members_and_blocks_to_archives", lambda *a, **k: None
    )

    asyncio.run(processor.process_release(20, "seen-show", refresh_qb_meta=True))

    al.get_release.assert_awaited()
    persist.assert_called()
    assert any(
        "members" in (c.args[1] if len(c.args) > 1 else {})
        for c in persist.call_args_list
    )
