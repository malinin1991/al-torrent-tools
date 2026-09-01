"""E2E-стиль: HEVC-фильтры через list_release_groups и HTML /releases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.templating import Jinja2Templates

from app.services.hevc_pairing import HEVC_SLA_HOURS
from app.services.releases_view import format_torrent_files_summary, list_release_groups
from app.utils.datetime_fmt import as_utc_iso, utcnow

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"


def _qj(*, rip_type: str, quality: str, codec: str) -> dict:
    return {
        "type": {"value": rip_type},
        "quality": {"value": quality},
        "codec": {"label": codec, "value": f"x/{codec}"},
    }


def _archive(
    *,
    archive_id: int,
    release_id: int,
    torrent_id: int,
    episodes: str,
    codec: str,
    rip_type: str = "BDRip",
    quality: str = "1080p",
    created_at: datetime | None = None,
    api_created_at: datetime | None = None,
    anime_name: str = "One Piece",
    release_alias: str = "one-piece",
    api_present: bool = True,
    superseded: bool = False,
    ignore_hevc: bool = False,
    info_hash: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=archive_id,
        release_id=release_id,
        torrent_id=torrent_id,
        info_hash=info_hash or (f"{archive_id:02x}" * 20)[:40],
        release_alias=release_alias,
        anime_name=anime_name,
        category="AniLibria/2024",
        torrent_type=f"{rip_type} {quality} {codec}",
        torrent_description=episodes,
        file_size=1024,
        created_at=created_at,
        api_created_at=api_created_at,
        quality_json=_qj(rip_type=rip_type, quality=quality, codec=codec),
        api_present=api_present,
        superseded=superseded,
        ignore_hevc=ignore_hevc,
    )


def _setup_list_db(
    *,
    pairing_rows: list,
    page_archives: list,
    stats_rows: list,
    total: int,
    tracked: list | None = None,
) -> MagicMock:
    db = MagicMock()
    execute_n = {"n": 0}

    def _execute(stmt):  # noqa: ANN001
        execute_n["n"] += 1
        result = MagicMock()
        if execute_n["n"] == 1:
            result.all.return_value = pairing_rows
        else:
            result.all.return_value = stats_rows
        return result

    db.execute.side_effect = _execute
    db.scalar.return_value = total
    db.scalars.side_effect = [
        MagicMock(all=lambda: page_archives),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: tracked or []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: page_archives),  # archives for events legacy
    ]
    return db


def test_exact_bdrip_pair_not_in_hevc_filters() -> None:
    now = utcnow()
    rows = [
        _archive(
            archive_id=1,
            release_id=10,
            torrent_id=100,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=30),
        ),
        _archive(
            archive_id=2,
            release_id=10,
            torrent_id=101,
            episodes="1-12",
            codec="HEVC",
            created_at=now,
        ),
    ]
    db = MagicMock()
    db.execute.return_value.all.return_value = rows

    missing = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    overdue = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert missing["groups"] == []
    assert overdue["groups"] == []
    assert missing["total"] == 0
    db.scalar.assert_not_called()


def test_near_miss_episodes_and_one_piece_ranges() -> None:
    """AVC 347-350 + HEVC 347-349 → не missing (общий старт); 300-346 paired."""
    now = utcnow()
    avc_same_start = _archive(
        archive_id=1,
        release_id=10,
        torrent_id=100,
        episodes="347-350",
        codec="AVC",
        created_at=now - timedelta(hours=3),
    )
    avc_paired = _archive(
        archive_id=2,
        release_id=10,
        torrent_id=101,
        episodes="300-346",
        codec="AVC",
        created_at=now,
    )
    hevc_pair = _archive(
        archive_id=3,
        release_id=10,
        torrent_id=102,
        episodes="300-346",
        codec="HEVC",
        created_at=now,
    )
    hevc_near = _archive(
        archive_id=4,
        release_id=10,
        torrent_id=103,
        episodes="347-349",
        codec="HEVC",
        created_at=now,
    )
    avc_alone = _archive(
        archive_id=5,
        release_id=10,
        torrent_id=104,
        episodes="400-410",
        codec="AVC",
        created_at=now,
    )
    pairing = [avc_same_start, avc_paired, hevc_pair, hevc_near, avc_alone]
    stats = [SimpleNamespace(release_id=10, last_updated=now, torrent_count=5)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )

    result = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status is None  # start 347 закрыт HEVC 347-349
    assert by_id[2].hevc_pair_status is None
    assert by_id[3].hevc_pair_status is None
    assert by_id[4].hevc_pair_status is None
    assert by_id[5].hevc_pair_status == "missing"


def test_shorter_hevc_not_missing_but_overdue() -> None:
    """AVC 1-12 + HEVC 1-11 age>24h → overdue filter, не missing."""
    now = utcnow()
    avc = _archive(
        archive_id=1,
        release_id=99,
        torrent_id=900,
        episodes="1-12",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 2),
    )
    hevc = _archive(
        archive_id=2,
        release_id=99,
        torrent_id=901,
        episodes="1-11",
        codec="HEVC",
        created_at=now,
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=99, last_updated=now, torrent_count=2)]

    db_missing = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=0,
    )
    # missing: pairing query вернёт rows, но release_ids пустой → early empty
    # _setup всегда ставит scalar; для empty hevc filter list_release_groups
    # делает early return без scalar если нет matching ids.
    missing = list_release_groups(db_missing, hevc_filter="missing", page=1, per_page=30)
    assert missing["groups"] == []
    assert missing["total"] == 0

    db_overdue = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    overdue = list_release_groups(db_overdue, hevc_filter="overdue", page=1, per_page=30)
    assert len(overdue["groups"]) == 1
    by_id = {t.archive_id: t for t in overdue["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "overdue"
    assert by_id[2].hevc_pair_status is None


def test_ova_film_pairing_e2e() -> None:
    now = utcnow()
    avc_ova = _archive(
        archive_id=1,
        release_id=50,
        torrent_id=700,
        episodes="OVA 1-2",
        codec="AVC",
        created_at=now,
    )
    hevc_ova = _archive(
        archive_id=2,
        release_id=50,
        torrent_id=701,
        episodes="OVA",
        codec="HEVC",
        created_at=now,
    )
    avc_film = _archive(
        archive_id=3,
        release_id=50,
        torrent_id=702,
        episodes="Фильм",
        codec="AVC",
        created_at=now,
        anime_name="Movie Show",
    )
    # нет HEVC для фильма → missing
    pairing = [avc_ova, hevc_ova, avc_film]
    stats = [SimpleNamespace(release_id=50, last_updated=now, torrent_count=3)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status is None
    assert by_id[2].hevc_pair_status is None
    assert by_id[3].hevc_pair_status == "missing"


def test_pf_film_pair_not_missing_e2e() -> None:
    """release_id=8112-style: оба «П/ф фильм» BDRip 1080p — не в фильтре «Нет HEVC»."""
    now = utcnow()
    avc = _archive(
        archive_id=1,
        release_id=8112,
        torrent_id=9001,
        episodes="П/ф фильм",
        codec="AVC",
        created_at=now,
        anime_name="PF Movie",
        release_alias="pf-movie",
    )
    hevc = _archive(
        archive_id=2,
        release_id=8112,
        torrent_id=9002,
        episodes="П/ф фильм",
        codec="HEVC",
        created_at=now,
        anime_name="PF Movie",
        release_alias="pf-movie",
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=8112, last_updated=now, torrent_count=2)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    missing = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    assert missing["groups"] == []
    assert missing["total"] == 0


def test_pf_film_pairs_with_film_label_e2e() -> None:
    """«П/ф фильм» AVC + «Фильм» HEVC — не missing (один film start-key)."""
    now = utcnow()
    avc = _archive(
        archive_id=1,
        release_id=8113,
        torrent_id=9101,
        episodes="П/ф фильм",
        codec="AVC",
        created_at=now,
        anime_name="Mixed Film Labels",
        release_alias="mixed-film",
    )
    hevc = _archive(
        archive_id=2,
        release_id=8113,
        torrent_id=9102,
        episodes="Фильм",
        codec="HEVC",
        created_at=now,
        anime_name="Mixed Film Labels",
        release_alias="mixed-film",
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=8113, last_updated=now, torrent_count=2)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    missing = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    assert missing["groups"] == []
    assert missing["total"] == 0


def test_film_case_avc_hevc_not_missing_e2e() -> None:
    """«ФИЛЬМ» AVC + «Фильм» HEVC — не missing/overdue (один start + exact casefold)."""
    now = utcnow()
    old = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    pairing = [
        _archive(
            archive_id=1,
            release_id=8114,
            torrent_id=9201,
            episodes="ФИЛЬМ",
            codec="AVC",
            created_at=old,
            anime_name="Film Case",
            release_alias="film-case",
        ),
        _archive(
            archive_id=2,
            release_id=8114,
            torrent_id=9202,
            episodes="Фильм",
            codec="HEVC",
            created_at=old,
            anime_name="Film Case",
            release_alias="film-case",
        ),
    ]
    db = MagicMock()
    db.execute.return_value.all.return_value = pairing
    missing = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    overdue = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert missing["groups"] == []
    assert overdue["groups"] == []
    assert missing["total"] == 0
    assert overdue["total"] == 0
    db.scalar.assert_not_called()


def test_age_boundary_missing_vs_overdue() -> None:
    # list_release_groups считает age через utcnow() — якорим created_at к нему.
    now = utcnow()
    fresh = _archive(
        archive_id=1,
        release_id=11,
        torrent_id=200,
        episodes="1-2",
        codec="AVC",
        created_at=now - timedelta(hours=2),
        anime_name="Fresh Show",
        release_alias="fresh",
    )
    # Pure missing age>SLA — только missing, НЕ overdue
    old_alone = _archive(
        archive_id=2,
        release_id=12,
        torrent_id=201,
        episodes="1-2",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 1),
        anime_name="Old Show",
        release_alias="old",
    )
    # чуть меньше SLA — missing, не overdue (запас на тик часов между utcnow-вызовами)
    edge = _archive(
        archive_id=3,
        release_id=13,
        torrent_id=202,
        episodes="1-2",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS - 1),
        anime_name="Edge Show",
        release_alias="edge",
    )
    # overdue: частичный HEVC + age>SLA
    overdue_avc = _archive(
        archive_id=4,
        release_id=14,
        torrent_id=203,
        episodes="1-12",
        codec="AVC",
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 1),
        anime_name="Catchup Show",
        release_alias="catchup",
    )
    overdue_hevc = _archive(
        archive_id=5,
        release_id=14,
        torrent_id=204,
        episodes="1-11",
        codec="HEVC",
        created_at=now,
        anime_name="Catchup Show",
        release_alias="catchup",
    )
    missing_pairing = [fresh, old_alone, edge]
    overdue_pairing = [overdue_avc, overdue_hevc]
    all_pairing = missing_pairing + overdue_pairing

    # overdue: только release 14 (presence + age>SLA); pure missing не попадает
    db_overdue = MagicMock()
    execute_n = {"n": 0}

    def _exec_overdue(stmt):  # noqa: ANN001
        execute_n["n"] += 1
        result = MagicMock()
        if execute_n["n"] == 1:
            result.all.return_value = all_pairing
        else:
            result.all.return_value = [
                SimpleNamespace(
                    release_id=14, last_updated=overdue_avc.created_at, torrent_count=2
                )
            ]
        return result

    db_overdue.execute.side_effect = _exec_overdue
    db_overdue.scalar.return_value = 1
    db_overdue.scalars.side_effect = [
        MagicMock(all=lambda: overdue_pairing),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: overdue_pairing),  # archives for events
    ]
    overdue = list_release_groups(db_overdue, hevc_filter="overdue", page=1, per_page=30)
    assert [g.release_id for g in overdue["groups"]] == [14]
    by_id = {t.archive_id: t for t in overdue["groups"][0].torrents}
    assert by_id[4].hevc_pair_status == "overdue"
    assert by_id[5].hevc_pair_status is None

    # missing: только pure missing (11/12/13); overdue-релиз 14 не в missing
    db_missing = MagicMock()
    exec_m = {"n": 0}

    def _exec_missing(stmt):  # noqa: ANN001
        exec_m["n"] += 1
        result = MagicMock()
        if exec_m["n"] == 1:
            result.all.return_value = all_pairing
        else:
            result.all.return_value = [
                SimpleNamespace(release_id=11, last_updated=fresh.created_at, torrent_count=1),
                SimpleNamespace(
                    release_id=12, last_updated=old_alone.created_at, torrent_count=1
                ),
                SimpleNamespace(release_id=13, last_updated=edge.created_at, torrent_count=1),
            ]
        return result

    db_missing.execute.side_effect = _exec_missing
    db_missing.scalar.return_value = 3
    db_missing.scalars.side_effect = [
        MagicMock(all=lambda: missing_pairing),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: missing_pairing),  # archives for events
    ]
    missing = list_release_groups(db_missing, hevc_filter="missing", page=1, per_page=30)
    assert {g.release_id for g in missing["groups"]} == {11, 12, 13}
    by_rid = {g.release_id: g.torrents[0].hevc_pair_status for g in missing["groups"]}
    assert by_rid[11] == "missing"
    assert by_rid[12] == "missing"  # age>SLA без HEVC — всё ещё missing
    assert by_rid[13] == "missing"


def test_list_release_groups_overdue_age_from_api_created_at() -> None:
    """api_created_at → hevc_overdue_age_from_api=True в list_release_groups."""
    now = utcnow()
    # system created_at свежий (<SLA), api старый (>SLA) → overdue + age_from_api
    avc = _archive(
        archive_id=1,
        release_id=20,
        torrent_id=300,
        episodes="1-12",
        codec="AVC",
        created_at=now - timedelta(hours=2),
        api_created_at=now - timedelta(hours=HEVC_SLA_HOURS + 2),
        anime_name="API Age Show",
        release_alias="api-age",
    )
    hevc = _archive(
        archive_id=2,
        release_id=20,
        torrent_id=301,
        episodes="1-11",
        codec="HEVC",
        created_at=now,
        anime_name="API Age Show",
        release_alias="api-age",
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=20, last_updated=now, torrent_count=2)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "overdue"
    assert by_id[1].hevc_overdue_age_from_api is True
    assert by_id[2].hevc_pair_status is None


def test_list_release_groups_overdue_badge_hours_past_sla_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hevc_pair_age_hours = age−24 (≈36), не сырой age (~60), не 371 и не 36±7.

    TZ: upload = API 2026-07-24T16:07:58Z (naive UTC); now = 27.07 04:18 UTC
    (= 11:18 UTC+7). Нельзя подставлять wall-clock 23:07/11:18 как naive UTC.
    """
    frozen_now = datetime(2026, 7, 27, 4, 18, 0)  # naive UTC = 11:18 UTC+7
    avc_upload = datetime(2026, 7, 24, 16, 7, 58)  # naive UTC = API …T16:07:58.000Z
    monkeypatch.setattr("app.services.hevc_pairing.utcnow", lambda: frozen_now)

    avc = _archive(
        archive_id=1,
        release_id=21,
        torrent_id=400,
        episodes="1-12",
        codec="AVC",
        created_at=frozen_now - timedelta(hours=2),
        api_created_at=avc_upload,
        anime_name="Badge Hours Show",
        release_alias="badge-hours",
    )
    hevc = _archive(
        archive_id=2,
        release_id=21,
        torrent_id=399,
        episodes="1-11",
        codec="HEVC",
        created_at=frozen_now - timedelta(hours=1),
        anime_name="Badge Hours Show",
        release_alias="badge-hours",
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=21, last_updated=frozen_now, torrent_count=2)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    t = {row.archive_id: row for row in result["groups"][0].torrents}[1]
    assert t.hevc_pair_status == "overdue"
    assert t.hevc_overdue_age_from_api is True
    assert t.api_created_at == avc_upload
    assert t.hevc_pair_age_hours == pytest.approx(36.16722222222222)
    assert int(t.hevc_pair_age_hours or 0) == 36
    assert abs((t.hevc_pair_age_hours or 0) - 60) > 20
    assert abs((t.hevc_pair_age_hours or 0) - 371) > 100
    assert abs((t.hevc_pair_age_hours or 0) - 29) > 5
    assert abs((t.hevc_pair_age_hours or 0) - 43) > 5


def test_multi_avc_overdue_earliest_anchor_hours_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Несколько overdue AVC на одном start: бейдж от earliest (1-3), не от 1-4."""
    frozen_now = datetime(2026, 7, 27, 12, 0, 0)
    monkeypatch.setattr("app.services.hevc_pairing.utcnow", lambda: frozen_now)
    avc_13 = _archive(
        archive_id=1,
        release_id=30,
        torrent_id=301,
        episodes="1-3",
        codec="AVC",
        created_at=frozen_now - timedelta(hours=1),
        api_created_at=frozen_now - timedelta(hours=50),
        anime_name="Multi AVC Anchor",
        release_alias="multi-avc-anchor",
    )
    avc_14 = _archive(
        archive_id=2,
        release_id=30,
        torrent_id=302,
        episodes="1-4",
        codec="AVC",
        created_at=frozen_now - timedelta(hours=1),
        api_created_at=frozen_now - timedelta(hours=30),
        anime_name="Multi AVC Anchor",
        release_alias="multi-avc-anchor",
    )
    hevc_12 = _archive(
        archive_id=3,
        release_id=30,
        torrent_id=200,
        episodes="1-2",
        codec="HEVC",
        created_at=frozen_now - timedelta(hours=2),
        anime_name="Multi AVC Anchor",
        release_alias="multi-avc-anchor",
    )
    pairing = [avc_13, avc_14, hevc_12]
    stats = [SimpleNamespace(release_id=30, last_updated=frozen_now, torrent_count=3)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "overdue"
    assert by_id[2].hevc_pair_status == "overdue"
    assert by_id[3].hevc_pair_status is None
    for archive_id in (1, 2):
        hours = by_id[archive_id].hevc_pair_age_hours
        assert hours == pytest.approx(26.0)
        assert int(hours or 0) == 26
        # Не бейдж от 1-4 (30−24=6)
        assert abs((hours or 0) - 6) > 10


def test_webrip_webdl_type_mismatch_filter_e2e() -> None:
    """WEBRip AVC + WEB-DL HEVC → type_mismatch filter; age>SLA не в «Просрочка»."""
    now = utcnow()
    old = now - timedelta(hours=HEVC_SLA_HOURS + 5)
    avc = _archive(
        archive_id=1,
        release_id=10278,
        torrent_id=1,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=old,
    )
    hevc = _archive(
        archive_id=2,
        release_id=10278,
        torrent_id=2,
        episodes="1-4",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=old,
    )
    pairing = [avc, hevc]
    stats = [SimpleNamespace(release_id=10278, last_updated=now, torrent_count=2)]

    db_mismatch = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    mismatch = list_release_groups(
        db_mismatch, hevc_filter="type_mismatch", page=1, per_page=30
    )
    assert len(mismatch["groups"]) == 1
    by_id = {t.archive_id: t for t in mismatch["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "type_mismatch"
    assert by_id[2].hevc_pair_status is None

    db_overdue = _setup_list_db(
        pairing_rows=pairing,
        page_archives=[],
        stats_rows=[],
        total=0,
    )
    overdue = list_release_groups(db_overdue, hevc_filter="overdue", page=1, per_page=30)
    assert overdue["groups"] == []
    assert overdue["total"] == 0

    db_missing = _setup_list_db(
        pairing_rows=pairing,
        page_archives=[],
        stats_rows=[],
        total=0,
    )
    missing = list_release_groups(db_missing, hevc_filter="missing", page=1, per_page=30)
    assert missing["groups"] == []
    assert missing["total"] == 0


def test_overdue_inherits_anchor_from_superseded_avc_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Superseded AVC 1-3 якорит overdue на активном AVC 1-4 (10277-like)."""
    frozen_now = datetime(2026, 7, 29, 12, 0, 0)
    monkeypatch.setattr("app.services.hevc_pairing.utcnow", lambda: frozen_now)
    avc_13 = _archive(
        archive_id=10,
        release_id=10277,
        torrent_id=39129,
        episodes="1-3",
        codec="AVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=50),
        api_created_at=frozen_now - timedelta(hours=50),
        api_present=False,
        superseded=True,
        anime_name="Mystics",
        release_alias="mystics",
    )
    avc_14 = _archive(
        archive_id=20,
        release_id=10277,
        torrent_id=39237,
        episodes="1-4",
        codec="AVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=6),
        api_created_at=frozen_now - timedelta(hours=6),
        anime_name="Mystics",
        release_alias="mystics",
    )
    hevc_12 = _archive(
        archive_id=30,
        release_id=10277,
        torrent_id=39081,
        episodes="1-2",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=60),
        anime_name="Mystics",
        release_alias="mystics",
    )
    pairing = [avc_13, avc_14, hevc_12]
    # Listing грузит все archive релиза (в т.ч. superseded) — якорь из истории.
    page = [avc_13, avc_14, hevc_12]
    stats = [
        SimpleNamespace(release_id=10277, last_updated=frozen_now, torrent_count=2)
    ]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=page,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[20].hevc_pair_status == "overdue"
    assert by_id[20].hevc_pair_age_hours == pytest.approx(26.0)
    assert by_id[30].hevc_pair_status is None
    # Superseded 1-3 уходит в archived, без бейджа на inactive.
    archived_ids = {t.archive_id for t in result["groups"][0].archived_torrents}
    assert 10 in archived_ids


def test_overdue_inherits_anchor_from_superseded_ignore_hevc_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ignore_hevc на superseded 1-3 не сбрасывает overdue якорь у активного 1-4."""
    frozen_now = datetime(2026, 7, 29, 12, 0, 0)
    monkeypatch.setattr("app.services.hevc_pairing.utcnow", lambda: frozen_now)
    avc_13 = _archive(
        archive_id=10,
        release_id=10277,
        torrent_id=39129,
        episodes="1-3",
        codec="AVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=50),
        api_created_at=frozen_now - timedelta(hours=50),
        api_present=False,
        superseded=True,
        ignore_hevc=True,
        anime_name="Mystics",
        release_alias="mystics",
    )
    avc_14 = _archive(
        archive_id=20,
        release_id=10277,
        torrent_id=39237,
        episodes="1-4",
        codec="AVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=6),
        api_created_at=frozen_now - timedelta(hours=6),
        ignore_hevc=False,
        anime_name="Mystics",
        release_alias="mystics",
    )
    hevc_12 = _archive(
        archive_id=30,
        release_id=10277,
        torrent_id=39081,
        episodes="1-2",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=frozen_now - timedelta(hours=60),
        anime_name="Mystics",
        release_alias="mystics",
    )
    pairing = [avc_13, avc_14, hevc_12]
    page = [avc_13, avc_14, hevc_12]
    stats = [
        SimpleNamespace(release_id=10277, last_updated=frozen_now, torrent_count=2)
    ]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=page,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[20].hevc_pair_status == "overdue"
    assert by_id[20].hevc_pair_age_hours == pytest.approx(26.0)
    assert by_id[20].ignore_hevc is False


def test_webrip_does_not_pair_with_bdrip_across_families() -> None:
    now = utcnow()
    avc = _archive(
        archive_id=1,
        release_id=20,
        torrent_id=300,
        episodes="1-12",
        codec="AVC",
        rip_type="BDRip",
        created_at=now,
    )
    hevc_web = _archive(
        archive_id=2,
        release_id=20,
        torrent_id=301,
        episodes="1-12",
        codec="HEVC",
        rip_type="WEBRip",
        created_at=now,
    )
    pairing = [avc, hevc_web]
    stats = [SimpleNamespace(release_id=20, last_updated=now, torrent_count=2)]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
    )
    result = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "missing"
    assert by_id[2].hevc_pair_status is None


def test_search_hevc_tracked_pagination_compose() -> None:
    """search + hevc_filter + tracked_only + page отражены в ответе и SQL."""
    now = utcnow()
    unpaired = _archive(
        archive_id=1,
        release_id=42,
        torrent_id=500,
        episodes="5-6",
        codec="AVC",
        created_at=now - timedelta(hours=5),
        anime_name="Tracked Unpaired",
        release_alias="tracked-unpaired",
    )
    pairing = [unpaired]
    stats = [SimpleNamespace(release_id=42, last_updated=now, torrent_count=1)]
    tracked = [
        SimpleNamespace(release_id=42, enabled=True, source="ui"),
    ]
    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pairing,
        stats_rows=stats,
        total=1,
        tracked=tracked,
    )

    result = list_release_groups(
        db,
        search="Tracked",
        tracked_only=True,
        hevc_filter="missing",
        page=1,
        per_page=10,
    )
    assert result["search"] == "Tracked"
    assert result["tracked_only"] is True
    assert result["hevc_filter"] == "missing"
    assert result["page"] == 1
    assert result["per_page"] == 10
    assert result["total"] == 1
    assert len(result["groups"]) == 1
    assert result["groups"][0].tracked is True
    assert result["groups"][0].torrents[0].hevc_pair_status == "missing"

    count_stmt = db.scalar.call_args[0][0]
    sql = str(count_stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    assert "tracked_releases" in sql
    assert "enabled" in sql


def test_releases_html_includes_hevc_filter_and_badges() -> None:
    from app.services.releases_view import ReleaseGroup, ReleaseTorrentRow

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    group = ReleaseGroup(
        release_id=1,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        last_updated=now,
        torrent_count=2,
        release_url=None,
        admin_url=None,
        genres=[],
        torrents=[
            ReleaseTorrentRow(
                archive_id=1,
                torrent_id=10,
                info_hash="aa" * 20,
                torrent_type="BDRip 1080p AVC",
                torrent_description="1-2",
                file_size=100,
                file_size_label="100 B",
                created_at=now - timedelta(hours=30),
                api_created_at=now - timedelta(hours=30),
                pipeline_status=None,
                pipeline_error=None,
                hevc_pair_status="overdue",
                # Часы сверх SLA (age 30 − 24), не полный age.
                hevc_pair_age_hours=6.0,
                hevc_overdue_age_from_api=True,
            ),
            ReleaseTorrentRow(
                archive_id=2,
                torrent_id=11,
                info_hash="bb" * 20,
                torrent_type="BDRip 1080p AVC",
                torrent_description="3-4",
                file_size=100,
                file_size_label="100 B",
                created_at=now,
                api_created_at=None,
                pipeline_status=None,
                pipeline_error=None,
                hevc_pair_status="missing",
                hevc_pair_age_hours=2.0,
            ),
            ReleaseTorrentRow(
                archive_id=3,
                torrent_id=12,
                info_hash="cc" * 20,
                torrent_type="WEBRip 1080p AVC",
                torrent_description="1-4",
                file_size=100,
                file_size_label="100 B",
                created_at=now,
                pipeline_status=None,
                pipeline_error=None,
                hevc_pair_status="type_mismatch",
                hevc_pair_age_hours=3.0,
            ),
            ReleaseTorrentRow(
                archive_id=4,
                torrent_id=13,
                info_hash="dd" * 20,
                torrent_type="BDRip 1080p AVC",
                torrent_description="5-6",
                file_size=100,
                file_size_label="100 B",
                created_at=now - timedelta(hours=40),
                pipeline_status=None,
                pipeline_error=None,
                hevc_pair_status="overdue",
                hevc_pair_age_hours=16.0,
                hevc_overdue_age_from_api=False,
            ),
        ],
        archived_torrents=[
            ReleaseTorrentRow(
                archive_id=5,
                torrent_id=14,
                info_hash="ee" * 20,
                torrent_type="BDRip 1080p AVC",
                torrent_description="1-1",
                file_size=50,
                file_size_label="50 B",
                created_at=now - timedelta(days=10),
                api_created_at=now - timedelta(days=11),
                pipeline_status=None,
                pipeline_error=None,
                api_present=False,
            ),
        ],
        tracked=False,
        track_source=None,
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "releases.html",
        {
            "request": request,
            "groups": [group],
            "search": "",
            "tracked_only": False,
            "hevc_filter": "missing",
            "show_hidden": False,
            "page": 1,
            "per_page": 30,
            "total": 1,
            "total_pages": 1,
        },
    ).body.decode("utf-8")

    assert 'name="hevc_filter"' in html
    assert 'value="missing"' in html and "selected" in html
    assert "Нет HEVC" in html
    assert "Просрочка" in html
    assert "Расхождение типов" in html
    assert 'value="type_mismatch"' in html
    assert "badge-danger" in html and "просрочка 6ч" in html
    assert "badge-warn" in html and "просрочка 16ч" in html
    assert "badge-warn" in html and "нет HEVC" in html
    assert "badge-muted" in html and "расхождение типов" in html
    assert "hevc_filter=missing" in html or 'value="missing"' in html
    # Колонка AniLibria + узкая иконка скачивания (без широкой кнопки «Скачать»).
    assert 'title="Дата/время добавления по AniLiberty"' in html
    assert ">AniLiberty<" in html
    assert 'aria-label="Скачать"' in html
    assert 'class="btn btn-icon"' in html
    assert "/api/archive/1/download" in html
    assert "/api/archive/5/download" in html
    assert ">Скачать<" not in html
    assert 'datetime="' in html and "local-time" in html
    # null api_created_at → прочерк; непустой → time
    assert html.count(">—<") >= 1 or "—</td>" in html


def test_overdue_badge_orange_vs_red_in_type_cell() -> None:
    """Оранжевый (fallback) vs красный (api_created_at) бейдж просрочки."""
    from app.services.releases_view import ReleaseTorrentRow

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
    request = MagicMock()

    red = ReleaseTorrentRow(
        archive_id=1,
        torrent_id=10,
        info_hash="aa" * 20,
        torrent_type="BDRip 1080p AVC",
        torrent_description="1-2",
        file_size=1,
        file_size_label="1 B",
        created_at=None,
        pipeline_status=None,
        pipeline_error=None,
        hevc_pair_status="overdue",
        hevc_pair_age_hours=5.0,
        hevc_overdue_age_from_api=True,
    )
    orange = ReleaseTorrentRow(
        archive_id=2,
        torrent_id=11,
        info_hash="bb" * 20,
        torrent_type="BDRip 1080p AVC",
        torrent_description="3-4",
        file_size=1,
        file_size_label="1 B",
        created_at=None,
        pipeline_status=None,
        pipeline_error=None,
        hevc_pair_status="overdue",
        hevc_pair_age_hours=7.0,
        hevc_overdue_age_from_api=False,
    )
    red_html = templates.TemplateResponse(
        request, "partials/release_torrent_type_cell.html", {"t": red}
    ).body.decode("utf-8")
    orange_html = templates.TemplateResponse(
        request, "partials/release_torrent_type_cell.html", {"t": orange}
    ).body.decode("utf-8")
    assert 'class="badge badge-danger"' in red_html and "просрочка 5ч" in red_html
    assert 'class="badge badge-warn"' in orange_html and "просрочка 7ч" in orange_html
    assert "badge-danger" not in orange_html


def test_releases_page_passes_hevc_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import releases_page

    captured: dict = {}

    def _list(db, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {
            "groups": [],
            "search": kwargs.get("search") or "",
            "tracked_only": bool(kwargs.get("tracked_only")),
            "hevc_filter": kwargs.get("hevc_filter") or "",
            "page": kwargs.get("page") or 1,
            "per_page": 30,
            "total": 0,
            "total_pages": 1,
        }

    monkeypatch.setattr("app.main.list_release_groups", _list)
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: ctx,
    )
    ctx = releases_page(
        MagicMock(),
        search="piece",
        tracked_only="on",
        hevc_filter="overdue",
        page=2,
        db=MagicMock(),
    )
    assert captured["search"] == "piece"
    assert captured["tracked_only"] is True
    assert captured["hevc_filter"] == "overdue"
    assert captured["page"] == 2
    assert ctx["hevc_filter"] == "overdue"


def test_overdue_sort_ascending_then_paginate() -> None:
    """overdue: меньшая просрочка сверху; пагинация после сортировки."""
    now = utcnow()
    # release 1: ~10ч сверх SLA; release 2: ~2ч; release 3: ~5ч
    def _overdue_pair(rid: int, past_hours: float, updated: datetime):
        age = HEVC_SLA_HOURS + past_hours
        avc = _archive(
            archive_id=rid * 10,
            release_id=rid,
            torrent_id=rid * 100,
            episodes="1-12",
            codec="AVC",
            created_at=now - timedelta(hours=age),
            anime_name=f"Show {rid}",
            release_alias=f"show-{rid}",
        )
        hevc = _archive(
            archive_id=rid * 10 + 1,
            release_id=rid,
            torrent_id=rid * 100 + 1,
            episodes="1-11",
            codec="HEVC",
            created_at=now,
            anime_name=f"Show {rid}",
            release_alias=f"show-{rid}",
        )
        return [avc, hevc], SimpleNamespace(
            release_id=rid, last_updated=updated, torrent_count=2
        )

    pair1, stats1 = _overdue_pair(1, 10, now - timedelta(hours=1))
    pair2, stats2 = _overdue_pair(2, 2, now - timedelta(hours=3))
    pair3, stats3 = _overdue_pair(3, 5, now)
    pairing = pair1 + pair2 + pair3
    # SQL отдаёт в произвольном порядке (как max created_at); ожидаем 2→3→1.
    stats_all = [stats1, stats3, stats2]

    db = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pair2,  # page1 per_page=1 → только release 2
        stats_rows=stats_all,
        total=3,
    )
    page1 = list_release_groups(db, hevc_filter="overdue", page=1, per_page=1)
    assert page1["total"] == 3
    assert page1["total_pages"] == 3
    assert [g.release_id for g in page1["groups"]] == [2]

    db2 = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pair3,
        stats_rows=stats_all,
        total=3,
    )
    page2 = list_release_groups(db2, hevc_filter="overdue", page=2, per_page=1)
    assert [g.release_id for g in page2["groups"]] == [3]

    db3 = _setup_list_db(
        pairing_rows=pairing,
        page_archives=pair1,
        stats_rows=stats_all,
        total=3,
    )
    page3 = list_release_groups(db3, hevc_filter="overdue", page=3, per_page=1)
    assert [g.release_id for g in page3["groups"]] == [1]


def test_show_hidden_with_hevc_filter_includes_ignored() -> None:
    """show_hidden + missing: ignored AVC снова в выдаче; без фильтра — не влияет."""
    now = utcnow()
    ignored = _archive(
        archive_id=1,
        release_id=7,
        torrent_id=70,
        episodes="1-2",
        codec="AVC",
        created_at=now,
        anime_name="Hidden",
        release_alias="hidden",
    )
    ignored.ignore_hevc = True
    pairing = [ignored]
    stats = [SimpleNamespace(release_id=7, last_updated=now, torrent_count=1)]

    db_hidden = _setup_list_db(
        pairing_rows=pairing, page_archives=pairing, stats_rows=stats, total=1
    )
    without = list_release_groups(db_hidden, hevc_filter="missing", page=1, per_page=30)
    assert without["groups"] == []
    assert without["show_hidden"] is False

    db_show = _setup_list_db(
        pairing_rows=pairing, page_archives=pairing, stats_rows=stats, total=1
    )
    with_hidden = list_release_groups(
        db_show, hevc_filter="missing", show_hidden=True, page=1, per_page=30
    )
    assert with_hidden["show_hidden"] is True
    assert len(with_hidden["groups"]) == 1
    assert with_hidden["groups"][0].release_id == 7
    # Бейджи UI по-прежнему не ставятся на ignore_hevc (unpaired без include_ignored).
    assert with_hidden["groups"][0].torrents[0].hevc_pair_status is None
    assert with_hidden["groups"][0].torrents[0].ignore_hevc is True

    # Без HEVC-фильтра show_hidden no-op: релиз в общем списке и так виден.
    db_all = MagicMock()
    db_all.scalar.return_value = 1
    db_all.execute.return_value.all.return_value = stats
    db_all.scalars.side_effect = [
        MagicMock(all=lambda: pairing),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: pairing),  # archives for events
    ]
    general = list_release_groups(db_all, show_hidden=False, page=1, per_page=30)
    assert len(general["groups"]) == 1
    assert general["groups"][0].release_id == 7


def test_releases_html_show_hidden_members_and_blocks() -> None:
    from app.services.releases_view import ReleaseGroup, ReleaseTorrentRow

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    group = ReleaseGroup(
        release_id=1,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        last_updated=now,
        torrent_count=1,
        release_url=None,
        admin_url=None,
        genres=["Комедия"],
        members=[
            {"role": "voicing", "role_label": "Озвучка", "nickname": "Zvukar"},
            {"role": "timing", "role_label": "Тайминг", "nickname": "Timer"},
        ],
        is_blocked_by_geo=True,
        is_blocked_by_copyrights=True,
        torrents=[
            ReleaseTorrentRow(
                archive_id=1,
                torrent_id=10,
                info_hash="aa" * 20,
                torrent_type="BDRip 1080p AVC",
                torrent_description="1-2",
                file_size=100,
                file_size_label="100 B",
                created_at=now,
                pipeline_status=None,
                pipeline_error=None,
            ),
        ],
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "releases.html",
        {
            "request": request,
            "groups": [group],
            "search": "",
            "tracked_only": False,
            "hevc_filter": "overdue",
            "show_hidden": True,
            "page": 1,
            "per_page": 30,
            "total": 1,
            "total_pages": 1,
        },
    ).body.decode("utf-8")

    assert 'name="show_hidden"' in html
    assert "Отображать скрытое" in html
    assert "Сортировка — по длительности просрочки" in html
    assert 'name="show_hidden" value="on"' in html and "checked" in html
    assert "Геоблок" in html and "Копирасты" in html
    assert "badge-danger" in html
    assert 'member-tag--voicing' in html and "Zvukar" in html
    assert 'member-tag--timing' in html and "Timer" in html
    assert "Комедия" in html

    # Пагинация пробрасывает show_hidden.
    html_page = templates.TemplateResponse(
        request,
        "releases.html",
        {
            "request": request,
            "groups": [group],
            "search": "",
            "tracked_only": False,
            "hevc_filter": "overdue",
            "show_hidden": True,
            "page": 1,
            "per_page": 30,
            "total": 60,
            "total_pages": 2,
        },
    ).body.decode("utf-8")
    assert "show_hidden=on" in html_page
    assert "hevc_filter=overdue" in html_page


def test_releases_page_passes_show_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import releases_page

    captured: dict = {}

    def _list(db, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {
            "groups": [],
            "search": "",
            "tracked_only": False,
            "hevc_filter": kwargs.get("hevc_filter") or "",
            "show_hidden": bool(kwargs.get("show_hidden")),
            "page": 1,
            "per_page": 30,
            "total": 0,
            "total_pages": 1,
        }

    monkeypatch.setattr("app.main.list_release_groups", _list)
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: ctx,
    )
    releases_page(
        MagicMock(),
        hevc_filter="missing",
        show_hidden="on",
        db=MagicMock(),
    )
    assert captured["show_hidden"] is True
    assert captured["hevc_filter"] == "missing"
