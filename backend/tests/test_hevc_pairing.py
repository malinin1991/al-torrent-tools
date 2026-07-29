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
    episode_span,
    find_unpaired_avc,
    hevc_covers_avc_episodes,
    normalize_rip_type,
    overdue_hours_past_sla,
    release_ids_matching_hevc_filter,
    rip_family_key,
    sync_hevc_pair_events_for_release,
)

# TZ-контракт бейджа overdue (см. test_overdue_badge_hours_tz_al_api_z_utc_plus_7):
# API AL: UTC с Z → naive UTC в api_created_at; UI AL: wall-clock UTC+7;
# age = (utcnow − upload_utc) − 24. Оба конца в одной шкале → не ±7ч.
# Пример: API 2026-07-24T16:07:58.000Z (= 24.07 23:07 UTC+7),
# now 27.07 11:18 UTC+7 (= 27.07 04:18 UTC) → age≈60.17h → past SLA≈36.17h.
_UTC_PLUS_7 = timezone(timedelta(hours=7))
_BADGE_NOW = datetime(2026, 7, 27, 11, 18, 0, tzinfo=_UTC_PLUS_7)
_BADGE_AVC_UPLOAD = datetime(2026, 7, 24, 16, 7, 58)  # naive UTC = API …T16:07:58.000Z
_BADGE_AGE_HOURS = 60.16722222222222
_BADGE_PAST_SLA_HOURS = 36.16722222222222


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
    assert batch_start_key("ФИЛЬМ") == ("film",)
    assert batch_start_key("фильм") == ("film",)
    assert batch_start_key("Film") == ("film",)
    assert batch_start_key("FILM") == ("film",)
    assert batch_start_key("П/ф фильм") == ("film",)
    assert batch_start_key("П/Ф ФИЛЬМ") == ("film",)
    assert batch_start_key("п/ф") == ("film",)
    assert batch_start_key("п / ф фильм") == ("film",)
    assert batch_start_key("полнометражный фильм") == ("film",)
    assert batch_start_key("Полнометражный") == ("film",)
    assert batch_start_key("") is None
    assert batch_start_key("Specials") is None
    # Регистр не создаёт разные start-key.
    assert batch_start_key("ФИЛЬМ") == batch_start_key("Фильм") == ("film",)
    assert batch_start_key("OVA") == batch_start_key("ova") == ("ova", 1)


def test_episode_span_and_hevc_covers_range() -> None:
    assert episode_span("1-17") == (("regular", 1), 1, 17)
    assert episode_span("1-16") == (("regular", 1), 1, 16)
    assert episode_span("OVA 1-2") == (("ova", 1), 1, 2)
    assert episode_span("Фильм") == (("film",), 1, 1)
    # Инвертированный хвост: start-key как раньше от первого числа, lo/hi упорядочены.
    assert episode_span("10-5") == (("regular", 10), 5, 10)
    assert hevc_covers_avc_episodes("1-17", "1-16") is True
    assert hevc_covers_avc_episodes("1-16", "1-17") is False
    assert hevc_covers_avc_episodes("1-17", "1-17") is True
    assert hevc_covers_avc_episodes("OVA 1-2", "1-2") is False


def test_film_case_avc_hevc_not_missing() -> None:
    """AVC «ФИЛЬМ» + HEVC «Фильм» — один film start; не missing и не overdue."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="ФИЛЬМ", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="Фильм", codec="HEVC", created_at=now),
    ]
    assert find_unpaired_avc(rows, now=now) == []

    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    aged = [
        _row(archive_id=10, episodes="ФИЛЬМ", codec="AVC", created_at=old),
        _row(archive_id=11, episodes="Фильм", codec="HEVC", created_at=old),
    ]
    # exact_pair_key тоже casefold — регистр не держит catch-up/overdue.
    assert find_unpaired_avc(aged, now=now) == []


def test_ova_case_avc_hevc_not_missing() -> None:
    """AVC «OVA» + HEVC «ova» — тот же ova-start и exact после casefold."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    rows = [
        _row(archive_id=1, episodes="OVA", codec="AVC", created_at=old),
        _row(archive_id=2, episodes="ova", codec="HEVC", created_at=old),
    ]
    assert find_unpaired_avc(rows, now=now) == []


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
    """«П/ф фильм» и «Фильм» — один film start; покрытие диапазона закрывает catch-up."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="П/ф фильм", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="Фильм", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    # Раньше exact description различался → overdue после SLA; теперь film⊇film.
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    covered_rows = [
        _row(archive_id=10, episodes="П/ф фильм", codec="AVC", created_at=old),
        _row(archive_id=11, episodes="Фильм", codec="HEVC", created_at=old),
    ]
    assert find_unpaired_avc(covered_rows, now=now) == []
    assert release_ids_matching_hevc_filter(
        covered_rows, hevc_filter="overdue", now=now
    ) == set()
    assert release_ids_matching_hevc_filter(
        covered_rows, hevc_filter="missing", now=now
    ) == set()


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


def test_normalize_rip_type_web_variants() -> None:
    """WEBRip/WEB-DL канон: пробел, дефис, подчёркивание, слитное написание."""
    for raw in ("WEBRip", "WEB Rip", "WEB_Rip", "web-rip", "WEBRIP"):
        assert normalize_rip_type(raw) == "WEBRip"
    for raw in ("WEB-DL", "WEBDL", "WEB DL", "WEB_DL", "web-dl", "Web Dl"):
        assert normalize_rip_type(raw) == "WEB-DL"
    assert normalize_rip_type("BDRip") == "BDRip"


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
        rip_family_key(quality_json={}, torrent_type="WEB DL 1080p HEVC")
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
    """WEBRip AVC + WEB-DL HEVC age>24h → только type_mismatch, не overdue-фильтр."""
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
    assert unpaired[0].overdue is False
    assert unpaired[0].status == "type_mismatch"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()
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
    # Регрессия: ~60.17ч age → ~36.17ч past SLA (не ~60 и не 371).
    assert age_hours(_BADGE_AVC_UPLOAD, now=_BADGE_NOW) == pytest.approx(_BADGE_AGE_HOURS)
    assert overdue_hours_past_sla(_BADGE_AGE_HOURS) == pytest.approx(_BADGE_PAST_SLA_HOURS)
    assert int(overdue_hours_past_sla(_BADGE_AGE_HOURS) or 0) == 36
    past = overdue_hours_past_sla(_BADGE_AGE_HOURS) or 0.0
    assert abs(past - 60) > 20
    assert abs(past - 371) > 100


def test_overdue_badge_hours_tz_al_api_z_utc_plus_7() -> None:
    """TZ: API …Z → naive UTC; now UTC+7; past SLA≈36, не 36±7.

    AniLibria UI показывает UTC+7 (16:07Z → 23:07 local). Часы бейджа —
    (now − upload) в UTC минус 24ч. Если Z срезать без конвертации (23:07 как UTC)
    при корректном now → ~29ч; если wall-clock now принять за UTC при корректном
    upload → ~43ч. max(created_at, updated_at) остаётся источником upload.
    """
    from app.services.torrent_archive import TorrentArchiveService

    api_created = TorrentArchiveService._extract_api_created_at(
        {
            "created_at": "2026-07-10T16:52:16.000Z",
            "updated_at": "2026-07-24T16:07:58.000Z",
        }
    )
    assert api_created == datetime(2026, 7, 24, 16, 7, 58)

    now_local = datetime(2026, 7, 27, 11, 18, 0, tzinfo=_UTC_PLUS_7)
    rows = [
        _row(
            archive_id=1,
            torrent_id=100,
            episodes="1-12",
            codec="AVC",
            created_at=now_local.astimezone(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=2),
            api_created_at=api_created,
        ),
        _row(
            archive_id=2,
            torrent_id=99,
            episodes="1-11",
            codec="HEVC",
            created_at=now_local.astimezone(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=1),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now_local)
    assert len(unpaired) == 1
    past = overdue_hours_past_sla(unpaired[0].age_hours)
    assert past == pytest.approx(_BADGE_PAST_SLA_HOURS)
    assert int(past or 0) == 36
    # Явный запрет сдвига ±7ч от неверной TZ-интерпретации.
    assert abs((past or 0.0) - 29.0) > 5
    assert abs((past or 0.0) - 43.0) > 5


def test_overdue_badge_hours_past_sla_frozen_api_created_at() -> None:
    """Бейдж hevc_pair_age_hours = age(api)−24, не сырой age (~60) и не 371."""
    now_utc = _BADGE_NOW.astimezone(timezone.utc).replace(tzinfo=None)
    rows = [
        _row(
            archive_id=1,
            torrent_id=100,
            episodes="1-12",
            codec="AVC",
            # system created свежий — SLA только от api_created_at
            created_at=now_utc - timedelta(hours=2),
            api_created_at=_BADGE_AVC_UPLOAD,
        ),
        _row(
            archive_id=2,
            torrent_id=99,
            episodes="1-11",
            codec="HEVC",
            created_at=now_utc - timedelta(hours=1),
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
    assert badge_hours == pytest.approx(_BADGE_PAST_SLA_HOURS)
    assert int(badge_hours or 0) == 36
    assert badge_hours is not None
    assert abs(badge_hours - 60) > 20  # не сырой age
    assert abs(badge_hours - 371) > 100
    assert abs(badge_hours - 29) > 5  # не −7ч TZ skew
    assert abs(badge_hours - 43) > 5  # не +7ч TZ skew


def test_overdue_badge_hours_past_sla_frozen_system_created_at() -> None:
    """Те же часы через fallback system created_at; age_from_api=False."""
    now_utc = _BADGE_NOW.astimezone(timezone.utc).replace(tzinfo=None)
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
            created_at=now_utc - timedelta(hours=1),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=_BADGE_NOW)
    assert len(unpaired) == 1
    u = unpaired[0]
    assert u.overdue is True
    assert u.age_from_api is False
    assert u.age_hours == pytest.approx(_BADGE_AGE_HOURS)
    badge_hours = overdue_hours_past_sla(u.age_hours)
    assert badge_hours == pytest.approx(_BADGE_PAST_SLA_HOURS)
    assert int(badge_hours or 0) == 36


def test_multi_avc_overdue_hours_from_earliest_batch_anchor() -> None:
    """HEVC 1-2 + AVC 1-3/1-4: age якоря = earliest (1-3), не более новый 1-4."""
    now = datetime(2026, 7, 27, 12, 0, 0)
    avc_13_upload = now - timedelta(hours=50)
    avc_14_upload = now - timedelta(hours=30)
    rows = [
        _row(
            archive_id=1,
            torrent_id=201,
            episodes="1-3",
            codec="AVC",
            created_at=now - timedelta(hours=1),
            api_created_at=avc_13_upload,
        ),
        _row(
            archive_id=2,
            torrent_id=202,
            episodes="1-4",
            codec="AVC",
            created_at=now - timedelta(hours=1),
            api_created_at=avc_14_upload,
        ),
        _row(
            archive_id=3,
            torrent_id=100,
            episodes="1-2",
            codec="HEVC",
            created_at=now - timedelta(hours=2),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert set(by_id) == {1, 2}
    for u in by_id.values():
        assert u.missing is False
        assert u.overdue is True
        assert u.age_hours == pytest.approx(50.0)
        assert u.created_at == avc_13_upload
        badge = overdue_hours_past_sla(u.age_hours)
        assert badge == pytest.approx(26.0)
        assert int(badge or 0) == 26
    # Не от часов 1-4 (30−24=6)
    assert int(overdue_hours_past_sla(30.0) or 0) == 6


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
    """Legacy overdue+missing → type_mismatch бейдж (priority) + новая запись."""
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
    assert recorded[0]["to_status"] == "type_mismatch"
    assert recorded[0]["details"]["missing"] is False
    assert recorded[0]["details"]["type_mismatch"] is True
    assert recorded[0]["details"]["overdue"] is False
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


def test_mebius_dust_overdue_hours_past_sla_not_371() -> None:
    """Bug A: api_created_at = updated_at (24.07), not first created (10.07) → ~36ч past SLA.

    Frozen now 27.07 11:18 UTC+7 = 27.07 04:18 UTC.
    AVC upload 24.07 16:07 UTC (= 23:07 UTC+7) → age ≈ 60.17h → past SLA ≈ 36.17h.
    Старый created_at давал бы ~371ч — регрессия.
    """
    now = datetime(2026, 7, 27, 4, 18, tzinfo=timezone.utc)
    avc_upload = datetime(2026, 7, 24, 16, 7, 58)  # naive UTC = AL updated_at
    stale_first_created = datetime(2026, 7, 10, 16, 52, 16)
    rows = [
        _row(
            archive_id=1,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_upload + timedelta(minutes=3),
            api_created_at=avc_upload,
            torrent_id=100,
        ),
        _row(
            archive_id=2,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=datetime(2026, 7, 18, 15, 45),
            api_created_at=datetime(2026, 7, 18, 15, 45),
            torrent_id=90,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].overdue is True
    assert unpaired[0].missing is False
    assert unpaired[0].age_from_api is True
    past = overdue_hours_past_sla(unpaired[0].age_hours)
    assert past is not None
    assert 35.0 < past < 37.0
    assert past != 371
    # Контроль: если бы взяли first created — получили бы ~371.
    stale_age = (now.replace(tzinfo=None) - stale_first_created).total_seconds() / 3600.0
    assert 370 < overdue_hours_past_sla(stale_age) < 372


def test_multi_avc_overdue_anchor_earliest_catchup() -> None:
    """Feature C: 1-2 exact OK; 1-3+1-4 catch-up → часы от earliest overdue (1-3)."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            torrent_id=10,
            episodes="1-2",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=datetime(2026, 7, 4, 12, 0),
            api_created_at=datetime(2026, 7, 4, 12, 0),
        ),
        _row(
            archive_id=2,
            torrent_id=11,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=datetime(2026, 7, 4, 13, 0),
            api_created_at=datetime(2026, 7, 4, 13, 0),
        ),
        _row(
            archive_id=3,
            torrent_id=20,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=datetime(2026, 7, 11, 12, 0),
            api_created_at=datetime(2026, 7, 11, 12, 0),
        ),
        _row(
            archive_id=4,
            torrent_id=30,
            episodes="1-4",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=datetime(2026, 7, 18, 12, 0),
            api_created_at=datetime(2026, 7, 18, 12, 0),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert 1 not in by_id  # exact HEVC 1-2
    assert by_id[3].overdue is True
    assert by_id[4].overdue is True
    # Оба от якоря 11.07 12:00 → age = 16d = 384h → past SLA = 360h
    for aid in (3, 4):
        past = overdue_hours_past_sla(by_id[aid].age_hours)
        assert past is not None
        assert abs(past - 360.0) < 0.01
        assert by_id[aid].created_at == datetime(2026, 7, 11, 12, 0)


def test_same_webdl_pair_not_type_mismatch() -> None:
    """WEB-DL AVC + WEB-DL HEVC same start → пара OK, не type_mismatch."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=now,
            torrent_id=1,
        ),
        _row(
            archive_id=2,
            episodes="1-3",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now,
            torrent_id=2,
        ),
    ]
    assert find_unpaired_avc(rows, now=now) == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="type_mismatch", now=now) == set()
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_webrip_avc_webdl_hevc_type_mismatch_not_missing_filter() -> None:
    """WEBRip AVC + WEB-DL HEVC same start+quality → type_mismatch, не missing."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1-3",
            codec="AVC",
            rip_type="WEBRip",
            created_at=now - timedelta(hours=2),
            torrent_id=1,
        ),
        _row(
            archive_id=2,
            episodes="1-3",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now,
            torrent_id=2,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].type_mismatch is True
    assert unpaired[0].missing is False
    assert unpaired[0].status == "type_mismatch"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="type_mismatch", now=now
    ) == {1}


def test_overdue_continues_on_avc_successor_after_supersede() -> None:
    """Release 10277-like: superseded AVC 1-3 overdue → активный 1-4 наследует якорь."""
    now = datetime(2026, 7, 29, 12, 0, 0)
    # AVC 1-3 вышел >SLA назад и опередил HEVC 1-2; затем заменён на 1-4 (<SLA сам).
    avc_13_upload = now - timedelta(hours=50)
    avc_14_upload = now - timedelta(hours=6)
    rows = [
        _row(
            archive_id=10,
            release_id=10277,
            torrent_id=39129,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_13_upload,
            api_created_at=avc_13_upload,
            api_present=False,
            superseded=True,
            info_hash="564175cafffd110e54c1e630360a323b82515ee7",
        ),
        _row(
            archive_id=20,
            release_id=10277,
            torrent_id=39237,
            episodes="1-4",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_14_upload,
            api_created_at=avc_14_upload,
            info_hash="8cc19487916545547330a25761e4ac34fb6c453f",
        ),
        _row(
            archive_id=30,
            release_id=10277,
            torrent_id=39081,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now - timedelta(hours=60),
            api_created_at=now - timedelta(hours=60),
            info_hash="741cf2d4df9e208fe3cfeec1b5472be9a61ef12d",
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    u = unpaired[0]
    assert u.archive_id == 20
    assert u.episodes == "1-4"
    assert u.missing is False
    assert u.overdue is True
    assert u.status == "overdue"
    # Якорь от дня эпизода 3 (50ч), не от свежего 1-4 (6ч).
    assert u.age_hours == pytest.approx(50.0)
    assert u.created_at == avc_13_upload
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {
        10277
    }
    # Без истории 1-3 активный 1-4 ещё в SLA — не overdue.
    without_hist = [r for r in rows if r.id != 10]
    assert find_unpaired_avc(without_hist, now=now) == []


def test_local_only_historical_anchor_does_not_override_active_api() -> None:
    """Gaikotsu-like: local-only якорь истории не даёт «просрочка 289ч» против свежего AL.

    Superseded AVC без api_created_at (torrent_id уже нет в list → backfill невозможен)
    + активный AVC с api ~3ч назад + частичный HEVC → age/бейдж от AL активного,
    внутри SLA → без overdue.
    """
    now = datetime(2026, 7, 29, 14, 0, 0)
    hist_local = now - timedelta(hours=289 + HEVC_SLA_HOURS)
    active_api = now - timedelta(hours=3)
    rows = [
        _row(
            archive_id=3295,
            release_id=10228,
            torrent_id=39000,
            episodes="1-3",
            codec="AVC",
            rip_type="WEBRip",
            created_at=hist_local,
            api_created_at=None,
            api_present=False,
            superseded=True,
        ),
        _row(
            archive_id=3463,
            release_id=10228,
            torrent_id=39239,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            created_at=active_api,
            api_created_at=active_api,
            info_hash="d64ab556b4a03a643f4dd809036845b92ef3efd9",
        ),
        _row(
            archive_id=3300,
            release_id=10228,
            torrent_id=39128,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEBRip",
            created_at=now - timedelta(hours=60),
            api_created_at=now - timedelta(hours=60),
            info_hash="d1f4cef6e730d7a6d5245a74bc568b549b111163",
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    # age от active api (3ч) < SLA → catch-up есть, но бейджа overdue ещё нет.
    assert unpaired == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()

    # После SLA от AL активного — overdue с красным бейджем, не 289ч.
    later = active_api + timedelta(hours=HEVC_SLA_HOURS + 5)
    unpaired_later = find_unpaired_avc(rows, now=later)
    assert len(unpaired_later) == 1
    u = unpaired_later[0]
    assert u.overdue is True
    assert u.age_from_api is True
    assert u.age_hours == pytest.approx(float(HEVC_SLA_HOURS + 5))
    assert int(overdue_hours_past_sla(u.age_hours) or 0) == 5


def test_api_historical_anchor_still_preferred_over_fresher_active_api() -> None:
    """Оба с api_created_at — earliest API (история 1-3), не свежий активный 1-4."""
    now = datetime(2026, 7, 29, 12, 0, 0)
    hist_api = now - timedelta(hours=50)
    active_api = now - timedelta(hours=6)
    rows = [
        _row(
            archive_id=10,
            torrent_id=100,
            episodes="1-3",
            codec="AVC",
            rip_type="WEBRip",
            created_at=hist_api,
            api_created_at=hist_api,
            api_present=False,
            superseded=True,
        ),
        _row(
            archive_id=20,
            torrent_id=200,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            created_at=active_api,
            api_created_at=active_api,
        ),
        _row(
            archive_id=30,
            torrent_id=90,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEBRip",
            created_at=now - timedelta(hours=60),
            api_created_at=now - timedelta(hours=60),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].age_hours == pytest.approx(50.0)
    assert unpaired[0].age_from_api is True
    assert unpaired[0].overdue is True


def test_iruma_avc_reupload_after_hevc_not_inherit_old_anchor() -> None:
    """Iruma-like: AVC→HEVC 1-17, затем re-upload AVC (>grace) — SLA от re-upload.

    Старый долг 1-16 (api ~171ч) не должен давать «просрочка 147ч», пока exact
    HEVC 1-17 есть и catch-up только из‑за более нового AVC torrent_id.
    """
    now = datetime(2026, 7, 29, 14, 30, 0)
    old_gap_api = now - timedelta(hours=147 + HEVC_SLA_HOURS)
    hevc_api = now - timedelta(hours=15)
    reupload_api = now - timedelta(hours=3)
    rows = [
        _row(
            archive_id=100,
            release_id=10161,
            torrent_id=39000,
            episodes="1-16",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=old_gap_api,
            api_created_at=old_gap_api,
            api_present=False,
            superseded=True,
        ),
        _row(
            archive_id=3457,
            release_id=10161,
            torrent_id=39234,
            episodes="1-17",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=hevc_api - timedelta(hours=1),
            api_created_at=hevc_api - timedelta(hours=1),
            api_present=False,
            superseded=True,
            info_hash="17896fe1472af7c0f9d017bd1f78d8d6de2b5c42",
        ),
        _row(
            archive_id=3458,
            release_id=10161,
            torrent_id=39235,
            episodes="1-17",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=hevc_api,
            api_created_at=hevc_api,
            info_hash="7d0613ba35bd5ac8fc85ddf3a81a14612d9013f3",
        ),
        _row(
            archive_id=3464,
            release_id=10161,
            torrent_id=39240,
            episodes="1-17",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=reupload_api,
            api_created_at=reupload_api,
            info_hash="278c9f5f8a642457177ee341c1f1bbba95e283e4",
        ),
    ]
    # 3ч < SLA — catch-up (hevc_outdated), но без бейджа overdue / без 147ч.
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()

    later = reupload_api + timedelta(hours=HEVC_SLA_HOURS + 4)
    unpaired_later = find_unpaired_avc(rows, now=later)
    assert len(unpaired_later) == 1
    u = unpaired_later[0]
    assert u.hevc_outdated is True
    assert u.overdue is True
    assert u.age_from_api is True
    assert u.age_hours == pytest.approx(float(HEVC_SLA_HOURS + 4))
    assert int(overdue_hours_past_sla(u.age_hours) or 0) == 4


def test_hevc_wider_range_clears_shorter_historical_catchup() -> None:
    """HEVC 1-17 покрывает исторический AVC 1-16 — без якоря от 1-16."""
    now = datetime(2026, 7, 29, 14, 0, 0)
    old = now - timedelta(hours=50)
    fresh = now - timedelta(hours=2)
    rows = [
        _row(
            archive_id=1,
            torrent_id=10,
            episodes="1-16",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=old,
            api_created_at=old,
            api_present=False,
            superseded=True,
        ),
        _row(
            archive_id=2,
            torrent_id=20,
            episodes="1-17",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=fresh,
            api_created_at=fresh,
        ),
        _row(
            archive_id=3,
            torrent_id=30,
            episodes="1-17",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=fresh,
            api_created_at=fresh,
        ),
    ]
    assert find_unpaired_avc(rows, now=now) == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()


def test_overdue_anchor_from_superseded_ignore_hevc() -> None:
    """Superseded AVC с ignore_hevc=True всё равно якорит активного преемника."""
    now = datetime(2026, 7, 29, 12, 0, 0)
    avc_13_upload = now - timedelta(hours=50)
    avc_14_upload = now - timedelta(hours=6)
    rows = [
        _row(
            archive_id=10,
            release_id=10277,
            torrent_id=39129,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_13_upload,
            api_created_at=avc_13_upload,
            api_present=False,
            superseded=True,
            ignore_hevc=True,
            info_hash="564175cafffd110e54c1e630360a323b82515ee7",
        ),
        _row(
            archive_id=20,
            release_id=10277,
            torrent_id=39237,
            episodes="1-4",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_14_upload,
            api_created_at=avc_14_upload,
            ignore_hevc=False,
            info_hash="8cc19487916545547330a25761e4ac34fb6c453f",
        ),
        _row(
            archive_id=30,
            release_id=10277,
            torrent_id=39081,
            episodes="1-2",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=now - timedelta(hours=60),
            api_created_at=now - timedelta(hours=60),
            info_hash="741cf2d4df9e208fe3cfeec1b5472be9a61ef12d",
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    u = unpaired[0]
    assert u.archive_id == 20
    assert u.ignore_hevc is False
    assert u.overdue is True
    assert u.status == "overdue"
    assert u.age_hours == pytest.approx(50.0)
    assert u.created_at == avc_13_upload
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {
        10277
    }
    # Активный с ignore — бейдж/фильтр закрыты; история по-прежнему не эмитит.
    rows[1].ignore_hevc = True
    assert find_unpaired_avc(rows, now=now) == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()


def test_type_mismatch_not_in_overdue_filter_bucket() -> None:
    """type_mismatch и overdue — разные бакеты фильтра; age>SLA не тянет в «Просрочка»."""
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 10)
    mismatch_rows = [
        _row(
            archive_id=1,
            release_id=10278,
            torrent_id=39192,
            episodes="1-4",
            codec="AVC",
            rip_type="WEBRip",
            created_at=old,
        ),
        _row(
            archive_id=2,
            release_id=10278,
            torrent_id=39199,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=old,
        ),
    ]
    pure_overdue = [
        _row(
            archive_id=3,
            release_id=50,
            torrent_id=10,
            episodes="1-12",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=old,
        ),
        _row(
            archive_id=4,
            release_id=50,
            torrent_id=9,
            episodes="1-11",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=old,
        ),
    ]
    rows = mismatch_rows + pure_overdue
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {50}
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="type_mismatch", now=now
    ) == {10278}
    mismatch = find_unpaired_avc(mismatch_rows, now=now)[0]
    assert mismatch.status == "type_mismatch"
    assert mismatch.overdue is False
    assert mismatch.type_mismatch is True
