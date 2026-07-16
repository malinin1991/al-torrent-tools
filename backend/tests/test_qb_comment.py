from unittest.mock import MagicMock

import pytest
from qbittorrentapi.exceptions import HTTP404Error, UnsupportedQbittorrentVersion

from app.services import qbittorrent as qb_mod
from app.services.qbittorrent import (
    _ensure_torrent_comment,
    collect_client_info_hashes,
    qb_add_torrent,
)


def _sample_torrent_bytes() -> bytes:
    info = b"d4:name8:test.bin6:lengthi1ee"
    return b"d8:announce14:http://tracker4:info" + info + b"e"


def test_ensure_comment_retries_404_then_overwrites(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.torrents_set_comment.side_effect = [
        HTTP404Error("not ready"),
        None,
    ]
    props = MagicMock()
    props.comment = "https://www.anilibria.top/anime/releases/release/x/torrents"
    client.torrents_properties.return_value = props
    monkeypatch.setattr(qb_mod.time, "sleep", lambda *_: None)

    ok = _ensure_torrent_comment(
        client,
        "a" * 40,
        "https://www.anilibria.top/anime/releases/release/x/torrents",
    )

    assert ok is True
    assert client.torrents_set_comment.call_count == 2


def test_ensure_comment_overwrites_existing_nonempty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Уже заполненный comment из .torrent должен быть перезаписан URL релиза."""
    client = MagicMock()
    props_new = MagicMock()
    props_new.comment = "https://www.anilibria.top/anime/releases/release/x/torrents"
    client.torrents_properties.side_effect = [props_new]
    monkeypatch.setattr(qb_mod.time, "sleep", lambda *_: None)

    ok = _ensure_torrent_comment(
        client,
        "b" * 40,
        "https://www.anilibria.top/anime/releases/release/x/torrents",
    )

    assert ok is True
    client.torrents_set_comment.assert_called_once_with(
        comment="https://www.anilibria.top/anime/releases/release/x/torrents",
        torrent_hashes="b" * 40,
    )


def test_ensure_comment_unsupported_version() -> None:
    client = MagicMock()
    client.torrents_set_comment.side_effect = UnsupportedQbittorrentVersion("old")
    qb_mod._comment_unsupported_warned = False

    assert _ensure_torrent_comment(client, "c" * 40, "https://example/x") is False


def test_ensure_comment_require_present_skips_missing() -> None:
    client = MagicMock()
    client.torrents_info.return_value = []

    assert (
        _ensure_torrent_comment(
            client,
            "d" * 40,
            "https://example/x",
            require_present=True,
        )
        is False
    )
    client.torrents_set_comment.assert_not_called()


def test_collect_client_info_hashes_includes_v1() -> None:
    client = MagicMock()
    t1 = MagicMock()
    t1.hash = "A" * 40
    t1.infohash_v1 = "B" * 40
    t1.infohash_v2 = None
    client.torrents_info.return_value = [t1]

    assert collect_client_info_hashes(client) == {"a" * 40, "b" * 40}


def test_qb_add_torrent_sets_comment_even_when_already_present(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.torrents_add.side_effect = qb_mod.qbittorrentapi.Conflict409Error("exists")
    ensure = MagicMock(return_value=True)
    monkeypatch.setattr(qb_mod, "_ensure_torrent_comment", ensure)
    monkeypatch.setattr(qb_mod, "_apply_torrent_rename", MagicMock())

    added_new = qb_add_torrent(
        client,
        _sample_torrent_bytes(),
        rename="Name",
        comment="https://www.anilibria.top/anime/releases/release/x/torrents",
        category="winter.2024",
    )

    assert added_new is False
    ensure.assert_called_once()
    assert ensure.call_args.args[2].startswith("https://www.anilibria.top/")
