"""Тесты пар AVC↔HEVC и фильтров missing/overdue."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    batch_start_key,
    classify_archive_codec,
    find_unpaired_avc,
    release_ids_matching_hevc_filter,
    rip_family_key,
)


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
    api_present: bool = True,
    superseded: bool = False,
    torrent_type: str | None = None,
    quality_json: dict | None = None,
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
        api_present=api_present,
        superseded=superseded,
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
    """AVC+HEVC оба «П/ф фильм» в одном rip_family — не missing."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="П/ф фильм", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="П/ф фильм", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []


def test_pf_film_pairs_with_film_label() -> None:
    """«П/ф фильм» и «Фильм» — один film start-key внутри rip_family."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    rows = [
        _row(archive_id=1, episodes="П/ф фильм", codec="AVC", created_at=now),
        _row(archive_id=2, episodes="Фильм", codec="HEVC", created_at=now),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    # overdue: exact description всё ещё разный — не трогаем exact-логику;
    # при age ≤ SLA overdue нет; при age > SLA AVC станет overdue, но не missing.
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


def test_rip_family_fallback_strips_codec_from_torrent_type() -> None:
    assert (
        rip_family_key(quality_json={}, torrent_type="WEBRip 1080p HEVC")
        == "WEBRip 1080p"
    )
    assert rip_family_key(quality_json=None, torrent_type="x265 HEVC") == ""


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


def test_overdue_when_age_over_sla_without_exact() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 1)
    fresh = now - timedelta(hours=2)
    rows = [
        _row(archive_id=1, episodes="1-10", codec="AVC", created_at=old),
        _row(archive_id=2, episodes="11-20", codec="AVC", created_at=fresh),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert by_id[1].overdue is True
    assert by_id[1].missing is True
    assert by_id[1].status == "overdue"
    assert by_id[1].age_hours is not None and by_id[1].age_hours > HEVC_SLA_HOURS
    assert by_id[2].overdue is False
    assert by_id[2].missing is True
    assert by_id[2].status == "missing"


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
    ]
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == {10, 20}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {10}
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
    """age == SLA → missing; age > SLA → overdue."""
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            archive_id=1,
            episodes="1",
            codec="AVC",
            created_at=now - timedelta(hours=HEVC_SLA_HOURS),
        ),
        _row(
            archive_id=2,
            episodes="2",
            codec="AVC",
            created_at=now - timedelta(hours=HEVC_SLA_HOURS, seconds=1),
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    by_id = {u.archive_id: u for u in unpaired}
    assert by_id[1].overdue is False
    assert by_id[1].status == "missing"
    assert by_id[2].overdue is True
    assert by_id[2].status == "overdue"


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
