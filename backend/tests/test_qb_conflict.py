from qbittorrentapi.exceptions import Conflict409Error

from app.services.qbittorrent import is_qb_torrent_already_present


def test_conflict409_is_already_present() -> None:
    assert is_qb_torrent_already_present(Conflict409Error("Conflict")) is True


def test_generic_error_is_not_already_present() -> None:
    assert is_qb_torrent_already_present(RuntimeError("Connection refused")) is False


def test_conflict_message_is_already_present() -> None:
    assert is_qb_torrent_already_present(Exception("Conflict")) is True
