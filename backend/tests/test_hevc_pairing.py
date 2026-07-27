"""Тесты пар AVC↔HEVC и фильтров missing/overdue/type_mismatch."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    age_hours,
    batch_start_key,
    classify_archive_codec,
    find_unpaired_avc,
    overdue_hours_past_sla,
    release_ids_matching_hevc_filter,
    rip_family_key,
    sync_hevc_pair_events_for_release,
)

# Реальный баг бейджа: age≈60ч показывали как 60/371 вместо age−24≈36.
_BADGE_NOW = datetime(2026, 7, 27, 11, 18, 0)  # naive UTC, как utcnow()
_BADGE_AVC_UPLOAD = datetime(2026, 7, 24, 23, 10, 7)
_BADGE_AGE_HOURS = 60.131388888888885
_BADGE_PAST_SLA_HOURS = 36.131388888888885


def _qj(*, rip_type: str, quality: str, codec: str) -> dict:
    return {
        "type": {"value": rip_type},
        "quality": {"value": quality},
        "codec": {"label": codec, "value": f"x/{codec}"},
    }


def _row(
    *,
    archive_id: int,
    release_id: int = 1,
    torrent_id: int = 10,
    episodes: str,
    codec: str,
    rip_type: str = "BDRip",
    quality: str = "1080p",
    created_at: datetime | None = None,
    api_created_at: datetime | None = None,
    api_present: bool = True,
    superseded: bool = False,
    torrent_type: str | None = None,
    quality_json: dict | None = None,
    info_hash: str | None = None,
    ignore_hevc: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=archive_id,
        release_id=release_id,
        torrent_id=torrent_id,
        torrent_description=episodes,
        torrent_type=torrent_type or f"{rip_type} {quality} {codec}",
        quality_json=quality_json
        if quality_json is not None
        else _qj(rip_type=rip_type, quality=quality, codec=codec),
        created_at=created_at,
        api_created_at=api_created_at,
        api_present=api_present,
        superseded=superseded,
        info_hash=info_hash or f"{archive_id:040x}",
        ignore_hevc=ignore_hevc,
    )


def test_batch_start_key_regular_ova_film() -> None:
    assert batch_start_key("1-12") == ("regular", 1)
    assert batch_start_key("347-350") == ("regular", 347)
    assert batch_start_key("1") == ("regular", 1)
    assert batch_start_key("OVA 1-2") == ("ova", 1)
    assert batch_start_key("ova 3") == ("ova", 3)
    assert batch_start_key("OVA") == ("ova", 1)
    assert batch_start_key("Фильм") == ("film",)
    assert batch_start_key("Film") == ("film",)
    assert batch_start_key("П/ф фильм") == ("film",)
    assert batch_start_key("п/ф") == ("film",)
    assert batch_start_key("п / ф фильм") == ("film",)
    assert batch_start_key("полнометражный фильм") == ("film",)
    assert batch_start_key("Полнометражный") == ("film",)
    assert batch_start_key("") is None
    assert batch_start_key("Specials") is None


def test_pf_film_avc_hevc_not_missing() -> None:
    """AVC+HEVC оба «П/ф фильм» — не missing."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="П/ф фильм", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="П/ф фильм", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []


def test_pf_film_pairs_with_film_label() -> None:
    """«П/ф фильм» и «Фильм» — один film start-key для presence."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="П/ф фильм", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="Фильм", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    # overdue: exact description всё ещё разный — при age > SLA overdue, не missing.
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    overdue_rows = [
        _row(archive_id=10, episodes="П/ф фильм", codec="AVC", created_at=old),
        _row(archive_id=11, episodes="Фильм", codec="HEVC", created_at=old),
    ]
    overdue_unpaired = find_unpaired_avc(overdue_rows, now=now)
    assert len(overdue_unpaired) == 1
    assert overdue_unpaired[0].archive_id == 10
    assert overdue_unpaired[0].missing is False
    assert overdue_unpaired[0].overdue is True


def test_rip_family_from_quality_json() -> None:
    assert (
        rip_family_key(
            quality_json=_qj(rip_type="BDRip", quality="1080p", codec="AVC"),
            torrent_type="BDRip 1080p AVC",
        )
        == "BDRip 1080p"
    )


def test_rip_family_strips_codec_from_quality_json_type() -> None:
    """Codec в type/quality quality_json вырезается так же, как из torrent_type."""
    assert (
        rip_family_key(
            quality_json={
                "type": {"value": "BDRip HEVC"},
                "quality": {"value": "1080p"},
                "codec": {"label": "HEVC"},
            },
            torrent_type="ignored",
        )
        == "BDRip 1080p"
    )


def test_rip_family_keeps_webrip_and_webdl_distinct() -> None:
    """WEBRip и WEB-DL — разные rip_family (без канона WEB)."""
    assert (
        rip_family_key(
            quality_json=_qj(rip_type="WEBRip", quality="1080p", codec="AVC"),
            torrent_type="WEBRip 1080p AVC",
        )
        == "WEBRip 1080p"
    )
    assert (
        rip_family_key(
            quality_json=_qj(rip_type="WEB-DL", quality="1080p", codec="HEVC"),
            torrent_type="WEB-DL 1080p HEVC",
        )
        == "WEB-DL 1080p"
    )
    assert (
        rip_family_key(quality_json={}, torrent_type="WEBDL 1080p HEVC")
        == "WEB-DL 1080p"
    )
    assert (
        rip_family_key(quality_json={}, torrent_type="WEBRip 1080p HEVC")
        == "WEBRip 1080p"
    )
    assert rip_family_key(quality_json=None, torrent_type="x265 HEVC") == ""


def test_saijo_webrip_avc_webdl_hevc_type_mismatch_not_missing() -> None:
    """Release 10278-like: AVC WEBRip + HEVC WEB-DL → type_mismatch, не missing."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=3377,
            release_id=10278,
            torrent_id=1,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            quality="1080p",
            created_at=now - timedelta(hours=3),
        ),
        _row(
            archive_id=3380,
            release_id=10278,
            torrent_id=2,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            quality="1080p",
            created_at=now,
        ),
    ]
    assert batch_start_key("1-4") == ("regular", 1)
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is False
    assert unpaired[0].type_mismatch is True
    assert unpaired[0].overdue is False
    assert unpaired[0].status == "type_mismatch"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="type_mismatch", now=now
    ) == {10278}


def test_webrip_webdl_type_mismatch_and_overdue_after_sla() -> None:
    """WEBRip AVC + WEB-DL HEVC age>24h → overdue бейдж, оба флага в фильтрах."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    rows = [
        _row(
            archive_id=1,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            created_at=old,
        ),
        _row(
            archive_id=2,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=old,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is False
    assert unpaired[0].type_mismatch is True
    assert unpaired[0].overdue is True
    assert unpaired[0].status == "overdue"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {1}
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="type_mismatch", now=now
    ) == {1}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_bdrip_still_distinct_from_web() -> None:
    """BDRip vs WEB* — разные продукты → missing, не type_mismatch."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-4",
            codec="AVC",
            rip_type="BDRip",
            created_at=now,
        ),
        _row(
            archive_id=2,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert [u.archive_id for u in unpaired] == [1]
    assert unpaired[0].rip_family == "BDRip 1080p"
    assert unpaired[0].missing is True
    assert unpaired[0].type_mismatch is False


def test_avc_newer_than_hevc_not_missing_until_sla() -> None:
    """Тот же start/exact: AVC republish новее HEVC, age≤24h → нет бейджа."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    hevc_ts = now - timedelta(hours=10)
    avc_ts = now - timedelta(hours=1)
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=avc_ts,
            info_hash="aa" * 20,
        ),
        _row(
            archive_id=2,
            episodes="1-12",
            codec="HEVC",
            created_at=hevc_ts,
            info_hash="bb" * 20,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_avc_newer_than_hevc_overdue_after_sla() -> None:
    """AVC новее exact-HEVC по torrent_id и age > 24h → overdue, не missing."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    hevc_ts = now - timedelta(hours=48)
    avc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    rows = [
        _row(archive_id=1, torrent_id=200, episodes="1-12", codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=100, episodes="1-12", codec="HEVC", created_at=hevc_ts),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is False
    assert unpaired[0].hevc_outdated is True
    assert unpaired[0].overdue is True
    assert unpaired[0].status == "overdue"
    assert unpaired[0].need_state == "overdue"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {1}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_hevc_newer_by_torrent_id_not_overdue_even_if_created_at_older() -> None:
    """HEVC с большим torrent_id, но более ранним ALTT created_at — не overdue."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    avc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    hevc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 40)
    rows = [
        _row(archive_id=1, torrent_id=50, episodes="1-12", codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=90, episodes="1-12", codec="HEVC", created_at=hevc_ts),
    ]
    assert find_unpaired_avc(rows, now=now) == []


def test_hevc_newer_or_equal_not_missing() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    avc_ts = now - timedelta(hours=5)
    rows_newer = [
        _row(archive_id=1, torrent_id=10, episodes="1-12", codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=20, episodes="1-12", codec="HEVC", created_at=now),
    ]
    assert find_unpaired_avc(rows_newer, now=now) == []
    rows_equal = [
        _row(archive_id=3, torrent_id=30, episodes="1-12", codec="AVC", created_at=now),
        _row(archive_id=4, torrent_id=40, episodes="1-12", codec="HEVC", created_at=now),
    ]
    assert find_unpaired_avc(rows_equal, now=now) == []


def test_classify_archive_codec_from_quality_and_type() -> None:
    assert (
        classify_archive_codec(
            quality_json=_qj(rip_type="WEBRip", quality="1080p", codec="HEVC"),
            torrent_type="WEBRip 1080p HEVC",
        )
        == "HEVC"
    )
    assert classify_archive_codec(quality_json={}, torrent_type="BDRip 1080p AVC") == "AVC"
    assert classify_archive_codec(quality_json={}, torrent_type="unknown") is None


def test_empty_keys_do_not_false_pair() -> None:
    """Пустой rip_family + пустые episodes не склеивают чужие торренты."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="",
            codec="AVC",
            created_at=now,
            torrent_type="x264",
            quality_json={},
        ),
        _row(
            archive_id=2,
            episodes="",
            codec="HEVC",
            created_at=now,
            torrent_type="x265 HEVC",
            quality_json={},
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].archive_id == 1
    assert unpaired[0].missing is True


def test_missing_same_start_shorter_hevc_counts() -> None:
    """AVC 1-12 + HEVC 1-11 → не missing (общий старт 1)."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="1-12", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="1-11", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_missing_avc_alone() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [_row(archive_id=1, episodes="1-12", codec="AVC", created_at=now)]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is True
    assert unpaired[0].overdue is False
    assert unpaired[0].status == "missing"


def test_missing_partial_batch_leaves_next_start() -> None:
    """AVC 1-12 + HEVC 1-12 + AVC 13-25 → missing из‑за 13-25."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="1-12", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="1-12", codec="HEVC", created_at=now),
        _row(archive_id=3, episodes="13-25", codec="AVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert [u.archive_id for u in unpaired] == [3]
    assert unpaired[0].missing is True


def test_overdue_exact_description_independent_of_missing() -> None:
    """AVC 1-12 + HEVC 1-11, age>24h → overdue, но не missing."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    rows = [
        _row(archive_id=1, episodes="1-12", codec="AVC", created_at=old),
        _row(archive_id=2, episodes="1-11", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is False
    assert unpaired[0].overdue is True
    assert unpaired[0].status == "overdue"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {1}


def test_find_unpaired_avc_example_case() -> None:
    """AVC 347-350 без HEVC; AVC 300-346 с парой — только первый missing."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, torrent_id=1, episodes="347-350", codec="AVC", created_at=now),
        _row(archive_id=2, torrent_id=2, episodes="300-346", codec="AVC", created_at=now),
        _row(archive_id=3, torrent_id=3, episodes="300-346", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].archive_id == 1
    assert unpaired[0].episodes == "347-350"
    assert unpaired[0].status == "missing"
    assert unpaired[0].overdue is False


def test_near_miss_same_start_not_missing() -> None:
    """HEVC 347-349 закрывает missing для AVC 347-350 (общий старт 347)."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="347-350", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="347-349", codec="HEVC", created_at=now),
    ]
    assert find_unpaired_avc(rows, now=now) == []


def test_avc_alone_aged_missing_not_overdue() -> None:
    """AVC only, нет HEVC на batch_start, age>24h → missing, НЕ overdue."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    fresh = now - timedelta(hours=2)
    rows = [
        _row(archive_id=1, episodes="1-10", codec="AVC", created_at=old),
        _row(archive_id=2, episodes="11-20", codec="AVC", created_at=fresh),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert by_id[1].overdue is False
    assert by_id[1].missing is True
    assert by_id[1].status == "missing"
    assert by_id[1].need_state == "missing"
    assert by_id[1].age_hours is not None and by_id[1].age_hours > HEVC_SLA_HOURS
    assert by_id[2].overdue is False
    assert by_id[2].missing is True
    assert by_id[2].status == "missing"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == {1}


def test_ignores_archived_and_superseded() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="1-12", codec="AVC", created_at=now),
        _row(
            archive_id=2,
            episodes="1-12",
            codec="HEVC",
            created_at=now,
            api_present=False,
        ),
        _row(
            archive_id=3,
            episodes="1-5",
            codec="AVC",
            created_at=now,
            superseded=True,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    # HEVC в архиве не считается парой; superseded AVC не в выборке
    assert [u.archive_id for u in unpaired] == [1]


def test_release_ids_matching_filters() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            release_id=10,
            episodes="1-2",
            codec="AVC",
            created_at=now - timedelta(hours=30),
        ),
        _row(
            archive_id=2,
            release_id=20,
            episodes="1-2",
            codec="AVC",
            created_at=now - timedelta(hours=3),
        ),
        _row(
            archive_id=3,
            release_id=30,
            episodes="1-2",
            codec="AVC",
            created_at=now,
        ),
        _row(
            archive_id=4,
            release_id=30,
            episodes="1-2",
            codec="HEVC",
            created_at=now,
        ),
        # release 15: частичный HEVC + age>SLA → overdue, не missing
        _row(
            archive_id=7,
            release_id=15,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=30),
            torrent_id=70,
        ),
        _row(
            archive_id=8,
            release_id=15,
            episodes="1-11",
            codec="HEVC",
            created_at=now,
            torrent_id=71,
        ),
        _row(
            archive_id=5,
            release_id=40,
            episodes="1-2",
            codec="AVC",
            rip_type="WEBRip",
            created_at=now,
        ),
        _row(
            archive_id=6,
            release_id=40,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now,
        ),
    ]
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == {10, 20}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {15}
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="type_mismatch", now=now
    ) == {40}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="", now=now) == set()


def test_exact_pair_excluded_from_both_filters() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=30),
        ),
        _row(archive_id=2, episodes="1-12", codec="HEVC", created_at=now),
    ]
    assert find_unpaired_avc(rows, now=now) == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()


def test_sla_boundary_exact_hours_not_overdue() -> None:
    """С presence HEVC: age == SLA → ok (нет бейджа); age > SLA → overdue."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            release_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=HEVC_SLA_HOURS),
            torrent_id=10,
        ),
        _row(
            archive_id=2,
            release_id=1,
            episodes="1-11",
            codec="HEVC",
            created_at=now,
            torrent_id=11,
        ),
        _row(
            archive_id=3,
            release_id=2,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=HEVC_SLA_HOURS, seconds=1),
            torrent_id=20,
        ),
        _row(
            archive_id=4,
            release_id=2,
            episodes="1-11",
            codec="HEVC",
            created_at=now,
            torrent_id=21,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert 1 not in by_id  # age == SLA → ещё не overdue, presence закрывает missing
    assert by_id[3].overdue is True
    assert by_id[3].missing is False
    assert by_id[3].status == "overdue"


def test_overdue_hours_past_sla_for_badge() -> None:
    """Бейдж показывает часы сверх SLA, не полный age AVC."""
    assert overdue_hours_past_sla(None) is None
    assert overdue_hours_past_sla(10.0) == 0.0
    assert overdue_hours_past_sla(float(HEVC_SLA_HOURS)) == 0.0
    assert overdue_hours_past_sla(float(HEVC_SLA_HOURS) + 2.5) == 2.5
    # Регрессия: 60.13ч age → 36.13ч past SLA (не ~60 и не 371).
    assert age_hours(_BADGE_AVC_UPLOAD, now=_BADGE_NOW) == pytest.approx(_BADGE_AGE_HOURS)
    assert overdue_hours_past_sla(_BADGE_AGE_HOURS) == pytest.approx(_BADGE_PAST_SLA_HOURS)
    assert int(overdue_hours_past_sla(_BADGE_AGE_HOURS) or 0) == 36
    past = overdue_hours_past_sla(_BADGE_AGE_HOURS) or 0.0
    assert abs(past - 60) > 20
    assert abs(past - 371) > 100


def test_overdue_badge_hours_past_sla_frozen_api_created_at() -> None:
    """Бейдж hevc_pair_age_hours = age(api)−24, не сырой age (~60) и не 371."""
    rows = [
        _row(
            archive_id=1,
            torrent_id=100,
            episodes="1-12",
            codec="AVC",
            # system created свежий — SLA только от api_created_at
            created_at=_BADGE_NOW - timedelta(hours=2),
            api_created_at=_BADGE_AVC_UPLOAD,
        ),
        _row(
            archive_id=2,
            torrent_id=99,
            episodes="1-11",
            codec="HEVC",
            created_at=_BADGE_NOW - timedelta(hours=1),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=_BADGE_NOW)
    assert len(unpaired) == 1
    u = unpaired[0]
    assert u.overdue is True
    assert u.age_from_api is True
    assert u.age_hours == pytest.approx(_BADGE_AGE_HOURS)
    # Тот же путь, что releases_view → hevc_pair_age_hours
    badge_hours = overdue_hours_past_sla(u.age_hours)
    assert badge_hours == pytest.approx(36.131388888888885)
    assert badge_hours == _BADGE_PAST_SLA_HOURS
    assert int(badge_hours or 0) == 36
    assert badge_hours is not None
    assert abs(badge_hours - 60) > 20  # не сырой age
    assert abs(badge_hours - 371) > 100


def test_overdue_badge_hours_past_sla_frozen_system_created_at() -> None:
    """Те же часы через fallback system created_at; age_from_api=False."""
    rows = [
        _row(
            archive_id=1,
            torrent_id=100,
            episodes="1-12",
            codec="AVC",
            created_at=_BADGE_AVC_UPLOAD,
            api_created_at=None,
        ),
        _row(
            archive_id=2,
            torrent_id=99,
            episodes="1-11",
            codec="HEVC",
            created_at=_BADGE_NOW - timedelta(hours=1),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=_BADGE_NOW)
    assert len(unpaired) == 1
    u = unpaired[0]
    assert u.overdue is True
    assert u.age_from_api is False
    assert u.age_hours == pytest.approx(_BADGE_AGE_HOURS)
    badge_hours = overdue_hours_past_sla(u.age_hours)
    assert badge_hours == pytest.approx(36.131388888888885)
    assert int(badge_hours or 0) == 36


def test_different_rip_families_do_not_pair() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            rip_type="BDRip",
            created_at=now,
        ),
        _row(
            archive_id=2,
            episodes="1-12",
            codec="HEVC",
            rip_type="WEBRip",
            created_at=now,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert [u.archive_id for u in unpaired] == [1]
    assert unpaired[0].rip_family == "BDRip 1080p"
    assert unpaired[0].missing is True
    assert unpaired[0].type_mismatch is False


def test_ova_and_film_start_pairing() -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="OVA 1-2", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="OVA", codec="HEVC", created_at=now),
        _row(archive_id=3, episodes="Фильм", codec="AVC", created_at=now),
        _row(archive_id=4, episodes="Film", codec="HEVC", created_at=now),
        _row(archive_id=5, episodes="OVA 5", codec="AVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    # 1 paired with OVA (start 1); 3 paired with Film; 5 alone (ova start 5)
    assert [u.archive_id for u in unpaired] == [5]
    assert unpaired[0].missing is True


def test_same_web_type_hevc_clears_type_mismatch() -> None:
    """WEBRip AVC + WEBRip HEVC → ок; WEB-DL HEVC рядом не ломает."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            created_at=now,
        ),
        _row(
            archive_id=2,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEBRip",
            created_at=now,
        ),
        _row(
            archive_id=3,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now,
        ),
    ]
    assert find_unpaired_avc(rows, now=now) == []


def test_sync_hevc_pair_events_emits_on_need_and_skips_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Первый sync → hevc_status «Нет HEVC»; повтор без смены state → 0."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    avc = _row(
        archive_id=1,
        release_id=99,
        torrent_id=55,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=now,
        info_hash="aa" * 20,
    )
    pipeline = SimpleNamespace(id=7, info_hash="aa" * 20, status="master_added")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append({"pipeline_id": pipeline_id, **kwargs})
        return SimpleNamespace(id=len(recorded), pipeline_id=pipeline_id, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)

    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc]
    db.scalar.side_effect = [pipeline, None]
    n = sync_hevc_pair_events_for_release(db, 99, job_id=3, now=now)
    assert n == 1
    assert recorded[0]["event_type"] == "hevc_status"
    assert recorded[0]["message"] == "Нет HEVC"
    assert recorded[0]["to_status"] == "missing"
    assert recorded[0]["details"]["rip_family"] == "WEBRip 1080p"
    assert recorded[0]["details"]["batch_start_key"] == ["regular", 1]

    db.scalar.side_effect = [pipeline, SimpleNamespace(to_status="missing")]
    n2 = sync_hevc_pair_events_for_release(db, 99, job_id=3, now=now)
    assert n2 == 0
    assert len(recorded) == 1


def test_sync_hevc_pair_events_clears_when_same_type_pair_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    avc = _row(
        archive_id=1,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=now - timedelta(hours=2),
        info_hash="aa" * 20,
    )
    hevc = _row(
        archive_id=2,
        episodes="1-4",
        codec="HEVC",
        rip_type="WEBRip",
        created_at=now,
        info_hash="bb" * 20,
        torrent_id=56,
    )
    pipeline = SimpleNamespace(id=7, info_hash="aa" * 20, status="done")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append({"pipeline_id": pipeline_id, **kwargs})
        return SimpleNamespace(id=1, pipeline_id=pipeline_id, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc, hevc]
    db.scalar.side_effect = [pipeline, SimpleNamespace(to_status="missing")]
    n = sync_hevc_pair_events_for_release(db, 1, now=now)
    assert n == 1
    assert recorded[0]["message"] == "HEVC пара найдена"
    assert recorded[0]["from_status"] == "missing"
    assert recorded[0]["to_status"] == "ok"


def test_sync_type_mismatch_message(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    avc = _row(
        archive_id=1,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=now - timedelta(hours=2),
        info_hash="aa" * 20,
    )
    hevc = _row(
        archive_id=2,
        episodes="1-4",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=now,
        info_hash="bb" * 20,
        torrent_id=77,
    )
    pipeline = SimpleNamespace(id=3, info_hash="aa" * 20, status="done")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc, hevc]
    db.scalar.side_effect = [pipeline, None]
    n = sync_hevc_pair_events_for_release(db, 1, now=now)
    assert n == 1
    assert recorded[0]["message"] == "Расхождение типов"
    assert recorded[0]["to_status"] == "type_mismatch"
    assert recorded[0]["details"]["type_mismatch"] is True
    assert recorded[0]["details"]["missing"] is False


def test_sync_emits_transition_missing_to_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Переход need→need (missing→overdue) при появлении частичного HEVC."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    avc = _row(
        archive_id=1,
        episodes="1-12",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 1),
        info_hash="aa" * 20,
        torrent_id=10,
    )
    hevc = _row(
        archive_id=2,
        episodes="1-11",
        codec="HEVC",
        created_at=now,
        info_hash="bb" * 20,
        torrent_id=11,
    )
    pipeline = SimpleNamespace(id=3, info_hash="aa" * 20, status="done")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc, hevc]
    db.scalar.side_effect = [pipeline, SimpleNamespace(to_status="missing")]
    n = sync_hevc_pair_events_for_release(db, 1, now=now)
    assert n == 1
    assert recorded[0]["from_status"] == "missing"
    assert recorded[0]["to_status"] == "overdue"
    assert recorded[0]["message"] == "Просрочка HEVC"
    assert recorded[0]["details"]["missing"] is False
    assert recorded[0]["details"]["overdue"] is True


def test_sync_aged_avc_alone_stays_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVC alone age>SLA остаётся missing — не эмитим ложный overdue."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    avc = _row(
        archive_id=1,
        episodes="1-12",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 1),
        info_hash="aa" * 20,
    )
    pipeline = SimpleNamespace(id=3, info_hash="aa" * 20, status="done")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc]
    db.scalar.side_effect = [
        pipeline,
        SimpleNamespace(
            to_status="missing",
            details_json={
                "missing": True,
                "overdue": False,
                "type_mismatch": False,
                "hevc_outdated": False,
                "paired_hevc_info_hash": None,
            },
        ),
    ]
    n = sync_hevc_pair_events_for_release(db, 1, now=now)
    assert n == 0
    assert recorded == []


def test_sync_emits_when_flags_change_same_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy overdue+missing → overdue+type_mismatch: тот же бейдж, новая запись."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    avc = _row(
        archive_id=1,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=old,
        info_hash="aa" * 20,
    )
    hevc = _row(
        archive_id=2,
        episodes="1-4",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=old,
        info_hash="bb" * 20,
        torrent_id=88,
    )
    pipeline = SimpleNamespace(id=3, info_hash="aa" * 20, status="done")
    recorded: list[dict] = []

    def fake_record(db, pipeline_id, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)

    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", fake_record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [avc, hevc]
    last = SimpleNamespace(
        to_status="overdue",
        details_json={
            "missing": True,
            "overdue": True,
            "type_mismatch": False,
            "hevc_outdated": False,
            "paired_hevc_info_hash": None,
        },
    )
    db.scalar.side_effect = [pipeline, last]
    n = sync_hevc_pair_events_for_release(db, 1, now=now)
    assert n == 1
    assert recorded[0]["to_status"] == "overdue"
    assert recorded[0]["details"]["missing"] is False
    assert recorded[0]["details"]["type_mismatch"] is True
    assert recorded[0]["details"]["paired_hevc_info_hash"] == "bb" * 20


def test_overdue_sla_uses_api_created_at_when_set() -> None:
    """SLA clock: при api_created_at age считается от него, не от system created_at."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    # system created_at свежий (<SLA), api_created_at старый (>SLA) → overdue + age_from_api
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=2),
            api_created_at=now - timedelta(hours=30),
        ),
        _row(archive_id=2, episodes="1-11", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].overdue is True
    assert unpaired[0].age_from_api is True
    assert unpaired[0].age_hours is not None
    assert unpaired[0].age_hours > HEVC_SLA_HOURS


def test_overdue_sla_falls_back_to_system_created_at() -> None:
    """Без api_created_at SLA от system created_at; age_from_api=False (оранжевый бейдж)."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=30),
            api_created_at=None,
        ),
        _row(archive_id=2, episodes="1-11", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].overdue is True
    assert unpaired[0].age_from_api is False


def test_fresh_api_created_at_not_overdue_despite_old_system_created() -> None:
    """api_created_at свежий (<SLA) — не overdue, даже если system created_at старый."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=48),
            api_created_at=now - timedelta(hours=2),
        ),
        _row(archive_id=2, episodes="1-11", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    # presence есть, exact нет, но age по api < SLA → только type? нет, BDRip same.
    # exact нет + presence → overdue только если age > SLA. Здесь age мал → ok.
    assert all(not u.overdue for u in unpaired)
    assert unpaired == [] or all(u.status != "overdue" for u in unpaired)
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()


def test_catchup_tiebreak_uses_system_created_not_api() -> None:
    """Catch-up tie-break (равный torrent_id): system created_at, не api_created_at."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    # AVC ALTT новее HEVC → catch-up; api старше — не отменяет.
    rows_outdated = [
        _row(
            archive_id=1,
            torrent_id=50,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=1),
            api_created_at=now - timedelta(hours=48),
        ),
        _row(
            archive_id=2,
            torrent_id=50,
            episodes="1-12",
            codec="HEVC",
            created_at=now - timedelta(hours=5),
        ),
    ]
    unpaired = find_unpaired_avc(rows_outdated, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].hevc_outdated is True
    assert unpaired[0].overdue is True
    assert unpaired[0].age_from_api is True

    # AVC ALTT старше HEVC → не catch-up; свежий api не должен отравлять сравнение.
    rows_ok = [
        _row(
            archive_id=3,
            torrent_id=60,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=10),
            api_created_at=now - timedelta(hours=1),
        ),
        _row(
            archive_id=4,
            torrent_id=60,
            episodes="1-12",
            codec="HEVC",
            created_at=now - timedelta(hours=2),
        ),
    ]
    assert find_unpaired_avc(rows_ok, now=now) == []
