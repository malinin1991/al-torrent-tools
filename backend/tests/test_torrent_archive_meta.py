from datetime import datetime

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


def test_extract_api_created_at_from_openapi_datetime() -> None:
    parsed = TorrentArchiveService._extract_api_created_at(
        {"created_at": "2021-09-21T11:45:00+00:00"}
    )
    assert parsed == datetime(2021, 9, 21, 11, 45, 0)


def test_extract_api_created_at_prefers_later_updated_at() -> None:
    """SLA clock = max(created_at, updated_at): republish не даёт «371ч» от первой заливки."""
    parsed = TorrentArchiveService._extract_api_created_at(
        {
            # mebius-dust-like: created << updated (UI AL показывает updated)
            "created_at": "2026-07-10T16:52:16+00:00",
            "updated_at": "2026-07-24T16:07:58+00:00",
        }
    )
    assert parsed == datetime(2026, 7, 24, 16, 7, 58)


def test_extract_api_created_at_z_suffix_and_invalid() -> None:
    assert TorrentArchiveService._extract_api_created_at(
        {"created_at": "2021-09-21T11:45:00Z"}
    ) == datetime(2021, 9, 21, 11, 45, 0)
    assert TorrentArchiveService._extract_api_created_at(
        {"updated_at": "2021-09-21T11:45:00Z"}
    ) == datetime(2021, 9, 21, 11, 45, 0)
    assert TorrentArchiveService._extract_api_created_at({"created_at": "not-a-date"}) is None
    assert TorrentArchiveService._extract_api_created_at({}) is None


def test_save_torrent_persists_api_created_at_on_create(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    db.scalar.return_value = None
    added: list = []
    db.add.side_effect = lambda obj: added.append(obj)

    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="c" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 9,
            "description": "1-2",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "AVC"},
            "size": {"value": 10},
            "created_at": "2021-09-21T11:45:00+00:00",
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert len(added) == 1
    assert added[0].api_created_at == datetime(2021, 9, 21, 11, 45, 0)


def test_save_torrent_persists_api_created_at_on_update(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    existing = SimpleNamespace(
        id=1,
        info_hash="d" * 40,
        torrent_id=9,
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
        api_created_at=None,
        ignore_hevc=False,
    )
    db.scalar.return_value = existing
    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="d" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 9,
            "description": "1-2",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "AVC"},
            "size": {"value": 10},
            "created_at": "2022-01-15T08:00:00Z",
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert existing.api_created_at == datetime(2022, 1, 15, 8, 0, 0)
    assert db.add.call_count == 0


def test_save_torrent_preserves_api_created_at_when_payload_omits(tmp_path) -> None:
    """Update без created_at в payload не затирает уже сохранённый api_created_at."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    kept = datetime(2021, 9, 21, 11, 45, 0)
    db = MagicMock()
    existing = SimpleNamespace(
        id=1,
        info_hash="e" * 40,
        torrent_id=9,
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
        api_created_at=kept,
        ignore_hevc=False,
    )
    db.scalar.return_value = existing
    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="e" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 9,
            "description": "1-2",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "AVC"},
            "size": {"value": 10},
            # created_at отсутствует
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert existing.api_created_at == kept


def test_save_torrent_preserves_api_created_at_when_parse_fails(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    kept = datetime(2021, 9, 21, 11, 45, 0)
    db = MagicMock()
    existing = SimpleNamespace(
        id=1,
        info_hash="f" * 40,
        torrent_id=9,
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
        api_created_at=kept,
        ignore_hevc=False,
    )
    db.scalar.return_value = existing
    svc = TorrentArchiveService(db)
    svc._storage_root = tmp_path
    svc.save_torrent(
        torrent_bytes=b"d4:infod4:name4:teste",
        info_hash="f" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 9,
            "description": "1-2",
            "created_at": "not-a-date",
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert existing.api_created_at == kept


def test_fill_missing_api_created_at_only_null_rows() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    null_row = SimpleNamespace(torrent_id=10, api_created_at=None)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [null_row]
    svc = TorrentArchiveService(db)
    n = svc.fill_missing_api_created_at(
        1,
        [
            {"id": 10, "created_at": "2021-09-21T11:45:00Z"},
            {"id": 11, "created_at": "2022-01-01T00:00:00Z"},
        ],
    )
    assert n == 1
    assert null_row.api_created_at == datetime(2021, 9, 21, 11, 45, 0)
    db.commit.assert_called_once()


def test_fill_missing_api_created_at_refreshes_stale_created_only() -> None:
    """Старый api_created_at (только created) обновляется более поздним updated_at."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    stale = SimpleNamespace(
        torrent_id=10, api_created_at=datetime(2026, 7, 10, 16, 52, 16)
    )
    fresh_enough = SimpleNamespace(
        torrent_id=11, api_created_at=datetime(2026, 7, 26, 12, 0, 0)
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [stale, fresh_enough]
    svc = TorrentArchiveService(db)
    n = svc.fill_missing_api_created_at(
        1,
        [
            {
                "id": 10,
                "created_at": "2026-07-10T16:52:16Z",
                "updated_at": "2026-07-24T16:07:58Z",
            },
            {
                "id": 11,
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-25T00:00:00Z",
            },
        ],
    )
    assert n == 1
    assert stale.api_created_at == datetime(2026, 7, 24, 16, 7, 58)
    assert fresh_enough.api_created_at == datetime(2026, 7, 26, 12, 0, 0)


def test_processor_torrents_include_has_created_at() -> None:
    from app.services.torrent_processor import TorrentProcessor

    assert "created_at" in TorrentProcessor.RELEASE_TORRENTS_INCLUDE
    # Паритет с telegram handler по ключевым meta-полям.
    for key in ("id", "description", "codec", "type", "quality", "updated_at", "created_at"):
        assert key in TorrentProcessor.RELEASE_TORRENTS_INCLUDE
