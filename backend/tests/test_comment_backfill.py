from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.torrent_processor import TorrentProcessor


def test_backfill_qb_comments_from_archive_sets_present_only(monkeypatch) -> None:
    db = MagicMock()
    archive = SimpleNamespace(
        info_hash="a" * 40,
        release_alias="lets-go-kaiki-gumi",
        quality_json={},
    )
    db.scalars.return_value.all.return_value = [archive]

    master = MagicMock()
    slave = MagicMock()
    processor = TorrentProcessor(db=db, job_id=1, client=MagicMock(base_url="https://anilibria.top"))
    monkeypatch.setattr(
        processor,
        "_qb_clients_for_meta",
        lambda: [("master", master), ("slave", slave)],
    )
    monkeypatch.setattr(
        "app.services.torrent_processor.collect_client_info_hashes",
        lambda client: {"a" * 40} if client is master else set(),
    )
    ensure = MagicMock(return_value=True)
    monkeypatch.setattr("app.services.torrent_processor._ensure_torrent_comment", ensure)
    monkeypatch.setattr(processor, "_add_log", lambda *a, **k: None)

    result = processor.backfill_qb_comments_from_archive()

    assert result["archives"] == 1
    assert result["updated"] == 1
    assert ensure.call_count == 1
    assert ensure.call_args.args[0] is master
    assert "lets-go-kaiki-gumi" in ensure.call_args.args[2]


def test_candidate_hashes_prefer_archive() -> None:
    db = MagicMock()
    db.scalars.return_value.all.return_value = ["f" * 40]
    processor = TorrentProcessor(db=db, job_id=1, client=MagicMock())
    seen = SimpleNamespace(info_hash="c" * 40)

    hashes = processor._candidate_hashes_for_torrent(
        torrent_id=10,
        api_hash="a" * 40,
        seen=seen,
    )

    assert hashes == ["f" * 40, "c" * 40, "a" * 40]
