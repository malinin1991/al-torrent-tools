import hashlib

import pytest

from app.services.qbittorrent import sanitize_info_hash, torrent_info_hash
from app.services.torrent_processor import TorrentProcessor


def _sample_torrent_bytes() -> tuple[bytes, bytes]:
    info_section = b"d4:name8:test.bin6:lengthi12345ee"
    torrent_bytes = b"d8:announce14:http://tracker4:info" + info_section + b"e"
    return torrent_bytes, info_section


def test_torrent_info_hash_uses_info_section(tmp_path) -> None:
    torrent_bytes, info_section = _sample_torrent_bytes()
    torrent_file = tmp_path / "sample.torrent"
    torrent_file.write_bytes(torrent_bytes)

    assert torrent_info_hash(torrent_file) == hashlib.sha1(info_section).hexdigest()


def test_torrent_info_hash_from_bytes() -> None:
    torrent_bytes, info_section = _sample_torrent_bytes()
    expected = hashlib.sha1(info_section).hexdigest()

    assert torrent_info_hash(torrent_bytes) == expected
    assert torrent_info_hash(torrent_bytes) != hashlib.sha1(torrent_bytes).hexdigest()


def test_sanitize_info_hash_accepts_hex() -> None:
    assert sanitize_info_hash("  ABCDEF0123456789abcdef0123456789ABCDEF01  ") == (
        "abcdef0123456789abcdef0123456789abcdef01"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "zz",
        "../etc/passwd",
        "abc",
        "g" * 40,
        "a" * 31,
        "a" * 65,
        "not-a-hash!",
    ],
)
def test_sanitize_info_hash_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        sanitize_info_hash(value)


def test_build_seen_exists_query_with_hash() -> None:
    query = TorrentProcessor.build_seen_exists_query(42, "a" * 40)
    sql = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
    where_part = sql.split("where", 1)[1]
    assert "torrent_id" in where_part
    assert "info_hash" in where_part
    assert " or " in where_part


def test_build_seen_exists_query_without_hash() -> None:
    query = TorrentProcessor.build_seen_exists_query(42, None)
    sql = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
    where_part = sql.split("where", 1)[1]
    assert "torrent_id" in where_part
    assert "info_hash" not in where_part
    assert " or " not in where_part
