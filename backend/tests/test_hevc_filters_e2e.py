"""E2E-стиль: HEVC-фильтры через list_release_groups и HTML /releases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.templating import Jinja2Templates

from app.services.hevc_pairing import HEVC_SLA_HOURS
from app.services.releases_view import list_release_groups
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
    anime_name: str = "One Piece",
    release_alias: str = "one-piece",
    api_present: bool = True,
    superseded: bool = False,
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
        quality_json=_qj(rip_type=rip_type, quality=quality, codec=codec),
        api_present=api_present,
        superseded=superseded,
        ignore_hevc=False,
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
    old = _archive(
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
    pairing = [fresh, old, edge]

    # overdue: только release 12
    db_overdue = MagicMock()
    db_overdue.execute.return_value.all.return_value = pairing
    execute_n = {"n": 0}

    def _exec_overdue(stmt):  # noqa: ANN001
        execute_n["n"] += 1
        result = MagicMock()
        if execute_n["n"] == 1:
            result.all.return_value = pairing
        else:
            result.all.return_value = [
                SimpleNamespace(release_id=12, last_updated=old.created_at, torrent_count=1)
            ]
        return result

    db_overdue.execute.side_effect = _exec_overdue
    db_overdue.scalar.return_value = 1
    db_overdue.scalars.side_effect = [
        MagicMock(all=lambda: [old]),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: [old]),
    ]
    overdue = list_release_groups(db_overdue, hevc_filter="overdue", page=1, per_page=30)
    assert [g.release_id for g in overdue["groups"]] == [12]
    assert overdue["groups"][0].torrents[0].hevc_pair_status == "overdue"

    # missing: все три unpaired
    db_missing = MagicMock()
    exec_m = {"n": 0}

    def _exec_missing(stmt):  # noqa: ANN001
        exec_m["n"] += 1
        result = MagicMock()
        if exec_m["n"] == 1:
            result.all.return_value = pairing
        else:
            result.all.return_value = [
                SimpleNamespace(release_id=11, last_updated=fresh.created_at, torrent_count=1),
                SimpleNamespace(release_id=12, last_updated=old.created_at, torrent_count=1),
                SimpleNamespace(release_id=13, last_updated=edge.created_at, torrent_count=1),
            ]
        return result

    db_missing.execute.side_effect = _exec_missing
    db_missing.scalar.return_value = 3
    db_missing.scalars.side_effect = [
        MagicMock(all=lambda: pairing),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: pairing),
    ]
    missing = list_release_groups(db_missing, hevc_filter="missing", page=1, per_page=30)
    assert {g.release_id for g in missing["groups"]} == {11, 12, 13}
    by_rid = {g.release_id: g.torrents[0].hevc_pair_status for g in missing["groups"]}
    assert by_rid[11] == "missing"
    assert by_rid[12] == "overdue"
    assert by_rid[13] == "missing"


def test_webrip_webdl_type_mismatch_filter_e2e() -> None:
    """WEBRip AVC + WEB-DL HEVC → type_mismatch filter, не missing."""
    now = utcnow()
    avc = _archive(
        archive_id=1,
        release_id=10278,
        torrent_id=1,
        episodes="1-4",
        codec="AVC",
        rip_type="WEBRip",
        created_at=now - timedelta(hours=3),
    )
    hevc = _archive(
        archive_id=2,
        release_id=10278,
        torrent_id=2,
        episodes="1-4",
        codec="HEVC",
        rip_type="WEB-DL",
        created_at=now,
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

    db_missing = _setup_list_db(
        pairing_rows=pairing,
        page_archives=[],
        stats_rows=[],
        total=0,
    )
    missing = list_release_groups(db_missing, hevc_filter="missing", page=1, per_page=30)
    assert missing["groups"] == []
    assert missing["total"] == 0


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
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    group = ReleaseGroup(
        release_id=1,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        last_updated=now,
        torrent_count=2,
        release_url=None,
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
                pipeline_status=None,
                pipeline_error=None,
                hevc_pair_status="overdue",
                hevc_pair_age_hours=30.0,
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
        ],
        archived_torrents=[],
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
    assert "badge-danger" in html and "просрочка" in html
    assert "badge-warn" in html and "нет HEVC" in html
    assert "badge-muted" in html and "расхождение типов" in html
    assert "hevc_filter=missing" in html or 'value="missing"' in html


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
