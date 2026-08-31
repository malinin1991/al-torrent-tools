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


def test_attach_release_names_sparse_block_flags_do_not_clobber() -> None:
    """Sparse payload с одним block-ключом не затирает второй флаг в quality_json."""
    quality = {
        "is_blocked_by_geo": True,
        "is_blocked_by_copyrights": True,
        "genres": ["Комедия"],
    }
    out = TorrentArchiveService._attach_release_names(
        quality,
        {"is_blocked_by_geo": False, "name": {"main": "Show"}},
    )
    assert out["is_blocked_by_geo"] is False
    assert out["is_blocked_by_copyrights"] is True
    assert out["names"]["main"] == "Show"


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
    # Миллисекунды + Z: UTC naive, не wall-clock UTC+7 (16:07Z ≠ 23:07 UTC).
    assert TorrentArchiveService._extract_api_created_at(
        {"updated_at": "2026-07-24T16:07:58.000Z"}
    ) == datetime(2026, 7, 24, 16, 7, 58)
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


def test_save_torrent_same_hash_does_not_move_sla_clock(tmp_path) -> None:
    """Update той же версии: более поздний updated_at не двигает api_created_at."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    kept = datetime(2026, 7, 10, 16, 52, 16)
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
        torrent_description="1-9",
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
        info_hash="d" * 40,
        release_id=1,
        release_alias="x",
        torrent_payload={
            "id": 9,
            "description": "1-10",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "AVC"},
            "size": {"value": 10},
            "created_at": "2026-07-10T16:52:16Z",
            "updated_at": "2026-08-31T08:00:00Z",
        },
        release_payload={"name": {"main": "Show"}, "year": 2024},
    )
    assert existing.api_created_at == kept
    assert existing.torrent_description == "1-10"


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


def test_fill_missing_api_created_at_same_hash_does_not_move_clock() -> None:
    """Same hash + уже есть clock → не двигать; null → заполнить; superseded не трогать."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    same_hash = "a" * 40
    kept = datetime(2026, 7, 10, 16, 52, 16)
    hist_clock = datetime(2026, 6, 1, 12, 0, 0)
    filled_at = datetime(2026, 7, 20, 0, 0, 0)
    active = SimpleNamespace(
        torrent_id=10,
        info_hash=same_hash,
        api_created_at=kept,
        superseded=False,
        api_present=True,
    )
    null_row = SimpleNamespace(
        torrent_id=11,
        info_hash="b" * 40,
        api_created_at=None,
        superseded=False,
        api_present=True,
    )
    superseded = SimpleNamespace(
        torrent_id=10,
        info_hash="c" * 40,
        api_created_at=hist_clock,
        superseded=True,
        api_present=False,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [active, null_row, superseded]
    svc = TorrentArchiveService(db)
    n = svc.fill_missing_api_created_at(
        1,
        [
            {
                "id": 10,
                "info_hash": same_hash,
                "created_at": "2026-07-10T16:52:16Z",
                "updated_at": "2026-07-24T16:07:58Z",
            },
            {
                "id": 11,
                "info_hash": "b" * 40,
                "created_at": "2026-07-20T00:00:00Z",
            },
        ],
    )
    assert n == 1
    assert active.api_created_at == kept
    assert null_row.api_created_at == filled_at
    assert superseded.api_created_at == hist_clock
    db.commit.assert_called_once()


def test_fill_missing_api_created_at_hash_mismatch_skips_old_row() -> None:
    """Другой hash в payload — старую активную строку не трогаем (ждём supersede)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    kept = datetime(2026, 7, 10, 16, 52, 16)
    old_row = SimpleNamespace(
        torrent_id=10,
        info_hash="a" * 40,
        api_created_at=kept,
        superseded=False,
        api_present=True,
    )
    null_old = SimpleNamespace(
        torrent_id=11,
        info_hash="c" * 40,
        api_created_at=None,
        superseded=False,
        api_present=True,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [old_row, null_old]
    svc = TorrentArchiveService(db)
    n = svc.fill_missing_api_created_at(
        1,
        [
            {
                "id": 10,
                "info_hash": "b" * 40,
                "created_at": "2026-07-10T16:52:16Z",
                "updated_at": "2026-07-24T16:07:58Z",
            },
            {
                "id": 11,
                "info_hash": "d" * 40,
                "created_at": "2026-07-20T00:00:00Z",
            },
        ],
    )
    assert n == 0
    assert old_row.api_created_at == kept
    assert null_old.api_created_at is None
    db.commit.assert_not_called()


def test_fill_missing_api_created_at_skips_superseded_same_torrent_id() -> None:
    """Reused torrent_id: hist 1-8 не получает clock активного 1-9 из list."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    hist_clock = datetime(2026, 8, 23, 12, 0, 0)
    fresh_clock = datetime(2026, 8, 30, 20, 23, 41)
    superseded = SimpleNamespace(
        torrent_id=39768,
        api_created_at=hist_clock,
        superseded=True,
        api_present=False,
    )
    archived = SimpleNamespace(
        torrent_id=39768,
        api_created_at=hist_clock,
        superseded=False,
        api_present=False,
    )
    active = SimpleNamespace(
        torrent_id=39768,
        api_created_at=None,
        superseded=False,
        api_present=True,
    )
    db = MagicMock()
    # Цикл сам отсекает inactive, даже если query вернул все строки.
    db.scalars.return_value.all.return_value = [superseded, archived, active]
    svc = TorrentArchiveService(db)
    n = svc.fill_missing_api_created_at(
        10273,
        [
            {
                "id": 39768,
                "created_at": "2026-07-04T17:34:04Z",
                "updated_at": "2026-08-30T20:23:41Z",
            }
        ],
    )
    assert n == 1
    assert superseded.api_created_at == hist_clock
    assert archived.api_created_at == hist_clock
    assert active.api_created_at == fresh_clock


def test_update_archive_meta_same_hash_keeps_sla_clock() -> None:
    """In-place смена description: мета обновляется, api_created_at той же версии нет."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    same_hash = "a" * 40
    kept = datetime(2026, 7, 10, 16, 52, 16)
    db = MagicMock()
    archive = SimpleNamespace(
        torrent_id=7,
        release_id=1,
        info_hash=same_hash,
        torrent_description="1-9",
        torrent_type="WEBRip 1080p AVC",
        anime_name="Show",
        category="AniLibria/2026",
        description=None,
        release_alias="show",
        quality_json={"type": {"value": "WEBRip"}},
        api_created_at=kept,
        api_present=True,
    )
    db.scalar.return_value = archive
    svc = TorrentArchiveService(db)

    status = svc.update_archive_meta_from_api_payload(
        release_id=1,
        torrent_payload={
            "id": 7,
            "info_hash": same_hash,
            "description": "1-10",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "AVC"},
            "created_at": "2026-07-10T16:52:16Z",
            "updated_at": "2026-08-31T08:00:00Z",
        },
    )

    assert status == "updated"
    assert archive.torrent_description == "1-10"
    assert archive.api_created_at == kept
    db.commit.assert_called_once()


def test_processor_torrents_include_has_created_at() -> None:
    from app.services.torrent_processor import TorrentProcessor

    assert "created_at" in TorrentProcessor.RELEASE_TORRENTS_INCLUDE
    # Паритет с telegram handler по ключевым meta-полям.
    for key in ("id", "description", "codec", "type", "quality", "updated_at", "created_at"):
        assert key in TorrentProcessor.RELEASE_TORRENTS_INCLUDE
