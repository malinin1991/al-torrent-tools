"""Инварианты подсчёта full_sync: updated ≠ сумма meta-ops."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.torrent_processor import TorrentProcessor


def test_merge_and_format_keep_meta_separate_from_updated() -> None:
    total = TorrentProcessor.empty_release_stats()
    part = {
        **TorrentProcessor.empty_release_stats(),
        "updated": 0,
        "skipped": 2,
        "comments": 3,
        "tags": 3,
        "renames": 1,
    }
    TorrentProcessor.merge_release_stats(total, part)
    TorrentProcessor.merge_release_stats(
        total,
        {
            **TorrentProcessor.empty_release_stats(),
            "updated": 1,
            "skipped": 0,
            "comments": 0,
            "tags": 0,
            "renames": 0,
        },
    )

    assert total["updated"] == 1
    assert total["skipped"] == 2
    assert total["comments"] == 3
    assert total["tags"] == 3
    assert total["renames"] == 1
    # Регрессия бага: updated не должен быть comments+tags+renames
    assert total["updated"] != total["comments"] + total["tags"] + total["renames"]

    text = TorrentProcessor.format_batch_summary("итого", total, releases=1)
    assert "обновлено=1" in text
    assert "comments=3" in text
    assert "tags=3" in text
    assert "renames=1" in text


def test_all_seen_meta_does_not_inflate_updated(monkeypatch) -> None:
    """all_seen + refresh_qb_meta: comments/tags/renames растут, updated остаётся 0."""
    db = MagicMock()
    al = MagicMock()
    al.base_url = "https://anilibria.top"
    al.get_torrents_for_release = AsyncMock(
        return_value=[
            {"id": 5, "info_hash": "a" * 40},
            {"id": 6, "info_hash": "b" * 40},
        ]
    )
    al.get_release = AsyncMock(
        return_value={"id": 20, "alias": "seen-show", "genres": [{"name": "Драма"}]}
    )

    processor = TorrentProcessor(db=db, job_id=1, client=al)
    db.scalar.return_value = SimpleNamespace(info_hash="a" * 40)
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
    archive_svc.update_archive_meta_from_api_payload.return_value = "noop"
    monkeypatch.setattr(
        "app.services.torrent_processor.TorrentArchiveService",
        lambda *_a, **_k: archive_svc,
    )
    monkeypatch.setattr(processor, "_refresh_qb_comments", MagicMock(return_value=4))
    monkeypatch.setattr(processor, "_refresh_qb_tags", MagicMock(return_value=4))
    monkeypatch.setattr(processor, "_refresh_qb_renames", MagicMock(return_value=1))
    monkeypatch.setattr(
        processor,
        "_resolve_genres_for_meta",
        AsyncMock(return_value=["Драма"]),
    )
    monkeypatch.setattr(processor, "_persist_release_ui_meta_from_payload", MagicMock())

    stats = asyncio.run(processor.process_release(20, "seen-show", refresh_qb_meta=True))

    assert stats["comments"] == 4
    assert stats["tags"] == 4
    assert stats["renames"] == 1
    assert stats["updated"] == 0
    assert stats["added"] == 0
    assert stats["skipped"] == 2
    assert stats["updated"] != stats["comments"] + stats["tags"] + stats["renames"]
