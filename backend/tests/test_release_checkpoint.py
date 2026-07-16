from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.release_checkpoint import (
    invalidate_release_checkpoint,
    should_skip_by_torrents_fingerprint,
    should_skip_unchanged,
    torrents_fingerprint,
)


def test_torrents_fingerprint_stable_order() -> None:
    a = torrents_fingerprint(
        [
            {"id": 2, "hash": "bb", "updated_at": "2024-01-02"},
            {"id": 1, "hash": "aa", "updated_at": "2024-01-01"},
        ]
    )
    b = torrents_fingerprint(
        [
            {"id": 1, "hash": "aa", "updated_at": "2024-01-01"},
            {"id": 2, "hash": "bb", "updated_at": "2024-01-02"},
        ]
    )
    assert a == b
    assert a == "1:aa:2024-01-01|2:bb:2024-01-02"


def test_should_skip_unchanged_requires_markers() -> None:
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        api_updated_at="u1",
        api_fresh_at="f1",
        torrents_fingerprint="1:aa:",
    )
    assert should_skip_unchanged(db, 1, updated_at=None, fresh_at=None) is False
    assert should_skip_unchanged(db, 1, updated_at="u1", fresh_at="f1") is True
    assert should_skip_unchanged(db, 1, updated_at="u2", fresh_at="f1") is False


def test_should_skip_unchanged_without_checkpoint() -> None:
    db = MagicMock()
    db.get.return_value = None
    assert should_skip_unchanged(db, 7, updated_at="u", fresh_at="f") is False


def test_should_skip_unchanged_empty_fingerprint_never_skips() -> None:
    """Релиз без торрентов / после invalidate — markers не должны скрывать ретрай."""
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        api_updated_at="u1",
        api_fresh_at="f1",
        torrents_fingerprint="",
    )
    assert should_skip_unchanged(db, 1, updated_at="u1", fresh_at="f1") is False


def test_invalidate_release_checkpoint_clears_fingerprint() -> None:
    row = SimpleNamespace(torrents_fingerprint="1:aa:")
    db = MagicMock()
    db.get.return_value = row
    invalidate_release_checkpoint(db, 42)
    assert row.torrents_fingerprint == ""
    db.commit.assert_called_once()


def test_should_skip_by_torrents_fingerprint() -> None:
    db = MagicMock()
    db.get.return_value = SimpleNamespace(torrents_fingerprint="1:aa:")
    assert should_skip_by_torrents_fingerprint(db, 1, "1:aa:") is True
    assert should_skip_by_torrents_fingerprint(db, 1, "1:bb:") is False
    assert should_skip_by_torrents_fingerprint(db, 1, "") is False
