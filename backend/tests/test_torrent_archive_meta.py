from app.services.torrent_archive import TorrentArchiveService


def test_extract_torrent_type_from_codec_label() -> None:
    payload = {"codec": {"label": "HEVC", "value": "x265/HEVC"}}
    assert TorrentArchiveService.extract_torrent_type(payload) == "HEVC"


def test_extract_torrent_type_combined() -> None:
    payload = {
        "type": {"value": "WEBRip", "description": "WEBRip"},
        "quality": {"value": "1080p", "description": "1080p"},
        "codec": {"label": "HEVC", "value": "x265/HEVC"},
    }
    assert TorrentArchiveService.extract_torrent_type(payload) == "WEBRip 1080p HEVC"


def test_extract_torrent_type_bdrip() -> None:
    payload = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "AVC", "value": "x264/AVC"},
    }
    assert TorrentArchiveService.extract_torrent_type(payload) == "BDRip 1080p AVC"


def test_extract_torrent_description() -> None:
    payload = {"description": "1-189"}
    assert TorrentArchiveService._extract_torrent_description(payload) == "1-189"


def test_build_category_anilibria_year() -> None:
    assert TorrentArchiveService._build_category({"year": 2026, "season": {"value": "summer"}}) == "AniLibria/2026"


def test_build_category_anilibria_without_year() -> None:
    assert TorrentArchiveService._build_category({}) == "AniLibria"


def test_save_torrent_supersedes_old_hash(tmp_path) -> None:
    """Смена info_hash у того же torrent_id сохраняет старую версию в истории."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    old = SimpleNamespace(
        id=1,
        info_hash="a" * 40,
        torrent_id=7,
        release_id=1,
        release_alias="x",
        api_present=True,
        superseded=False,
    )
    # active_same_hash=None, active_same_torrent=old
    db.scalar.side_effect = [None, old]
    added: list = []
    db.add.side_effect = lambda obj: added.append(obj)

    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="b" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 7,
            "description": "1-2",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "HEVC"},
            "size": {"value": 10},
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert old.superseded is True
    assert old.api_present is False
    assert len(added) == 1
    assert added[0].info_hash == "b" * 40
    assert added[0].api_present is True
    assert added[0].superseded is False


def test_save_torrent_does_not_resurrect_superseded_while_active_exists(tmp_path) -> None:
    """Повторный save старого hash не снимает superseded, если есть другая активная версия."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    active = SimpleNamespace(
        id=2,
        info_hash="b" * 40,
        torrent_id=7,
        release_id=1,
        release_alias="x",
        api_present=True,
        superseded=False,
        anime_name=None,
        category=None,
        description=None,
        torrent_description=None,
        torrent_type=None,
        quality_json={},
        file_path="",
        file_size=None,
    )
    # active_same_hash(a)=None, active_same_torrent=active (hash b)
    db.scalar.side_effect = [None, active]
    added: list = []
    db.add.side_effect = lambda obj: added.append(obj)

    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="a" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 7,
            "description": "1-2",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "HEVC"},
            "size": {"value": 10},
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    # Текущая active (b) ушла в историю; создана новая запись с hash a.
    assert active.superseded is True
    assert active.api_present is False
    assert len(added) == 1
    assert added[0].info_hash == "a" * 40
    assert added[0].superseded is False
