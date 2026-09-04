from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.releases_view import (
    _build_file_rows,
    _info_hashes_with_active_hash_job,
    _recent_events_by_info_hash,
    format_torrent_files_summary,
    list_release_groups,
)


def test_build_archive_page_rows_attaches_files() -> None:
    from app.services.releases_view import build_archive_page_rows

    db = MagicMock()
    archive = SimpleNamespace(
        id=5,
        anime_name="Show",
        release_alias="show",
        category="AniLibria/2024",
        torrent_type="HEVC",
        torrent_description="1-12",
        release_id=10,
        torrent_id=100,
        info_hash="aa" * 20,
        file_size=2048,
        created_at=None,
        api_present=True,
        superseded=False,
    )
    tf = SimpleNamespace(
        relative_path="ep01.mkv",
        size=10,
        selected=True,
        full_path="/media/ep01.mkv",
        info_hash="aa" * 20,
        ui_status="new",
    )
    db.scalars.side_effect = [
        MagicMock(all=lambda: [tf]),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: []),  # archives for events legacy
        MagicMock(all=lambda: []),  # disk hashes
        MagicMock(all=lambda: []),  # active hash jobs
    ]

    rows = build_archive_page_rows(db, [archive])
    assert len(rows) == 1
    assert rows[0].id == 5
    assert rows[0].file_size_label == "2.0 KB"
    assert rows[0].api_present is True
    assert len(rows[0].files) == 1
    assert rows[0].files[0].relative_path == "ep01.mkv"
    assert rows[0].files[0].status == "new"


def test_list_release_groups_exposes_genres() -> None:
    db = MagicMock()
    stats_row = SimpleNamespace(release_id=10, last_updated=None, torrent_count=1)
    db.scalar.return_value = 1
    db.execute.return_value.all.return_value = [stats_row]
    archive = SimpleNamespace(
        id=1,
        release_id=10,
        release_alias="test-show",
        anime_name="Тест",
        category="AniLibria/2024",
        torrent_id=100,
        info_hash="a" * 40,
        torrent_type="WEBRip",
        torrent_description="1-12",
        file_size=1024,
        created_at=None,
        quality_json={
            "genres": ["Комедия", "Романтика"],
            "members": [
                {"role": "voicing", "role_label": "Озвучка", "nickname": "Zvukar"},
            ],
            "is_blocked_by_geo": True,
            "is_blocked_by_copyrights": True,
        },
        api_present=True,
        superseded=False,
    )
    db.scalars.side_effect = [
        MagicMock(all=lambda: [archive]),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: [archive]),  # archives for events legacy map
    ]

    result = list_release_groups(db, page=1, per_page=30)

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group.genres == ["Комедия", "Романтика"]
    assert group.members == [
        {"role": "voicing", "role_label": "Озвучка", "nickname": "Zvukar"},
    ]
    assert group.is_blocked_by_geo is True
    assert group.is_blocked_by_copyrights is True
    assert group.tracked is False
    assert group.track_source is None
    assert group.torrents[0].api_present is True
    assert group.torrents[0].hevc_pair_status is None
    assert group.archived_torrents == []
    assert result["tracked_only"] is False
    assert result["hevc_filter"] == ""
    assert result["show_hidden"] is False


def test_list_release_groups_tracked_only_flag() -> None:
    db = MagicMock()
    db.scalar.return_value = 0
    db.execute.return_value.all.return_value = []

    result = list_release_groups(db, tracked_only=True, page=1, per_page=30)

    assert result["tracked_only"] is True
    assert result["hevc_filter"] == ""
    assert result["groups"] == []
    assert result["total"] == 0
    # count-запрос должен ограничивать enabled tracked_releases
    count_stmt = db.scalar.call_args[0][0]
    sql = str(count_stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    assert "tracked_releases" in sql
    assert "enabled" in sql


def test_list_release_groups_hevc_filter_missing_marks_unpaired() -> None:
    from datetime import datetime, timezone

    db = MagicMock()
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    quality_avc = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "AVC", "value": "x264/AVC"},
    }
    quality_hevc = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "HEVC", "value": "x265/HEVC"},
    }
    avc_missing = SimpleNamespace(
        id=1,
        release_id=10,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        torrent_id=100,
        info_hash="a" * 40,
        torrent_type="BDRip 1080p AVC",
        torrent_description="347-350",
        file_size=1024,
        created_at=now,
        quality_json=quality_avc,
        api_present=True,
        superseded=False,
    )
    avc_paired = SimpleNamespace(
        id=2,
        release_id=10,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        torrent_id=101,
        info_hash="b" * 40,
        torrent_type="BDRip 1080p AVC",
        torrent_description="300-346",
        file_size=1024,
        created_at=now,
        quality_json=quality_avc,
        api_present=True,
        superseded=False,
    )
    hevc_pair = SimpleNamespace(
        id=3,
        release_id=10,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        torrent_id=102,
        info_hash="c" * 40,
        torrent_type="BDRip 1080p HEVC",
        torrent_description="300-346",
        file_size=1024,
        created_at=now,
        quality_json=quality_hevc,
        api_present=True,
        superseded=False,
    )
    pairing_rows = [avc_missing, avc_paired, hevc_pair]
    stats_row = SimpleNamespace(release_id=10, last_updated=now, torrent_count=3)

    execute_calls = {"n": 0}

    def _execute(stmt):  # noqa: ANN001
        execute_calls["n"] += 1
        result = MagicMock()
        if execute_calls["n"] == 1:
            # лёгкий SELECT для hevc_filter
            result.all.return_value = pairing_rows
        else:
            result.all.return_value = [stats_row]
        return result

    db.execute.side_effect = _execute
    db.scalar.return_value = 1
    db.scalars.side_effect = [
        MagicMock(all=lambda: pairing_rows),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: pairing_rows),  # archives for events legacy
    ]

    result = list_release_groups(db, hevc_filter="missing", page=1, per_page=30)

    assert result["hevc_filter"] == "missing"
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    assert by_id[1].hevc_pair_status == "missing"
    assert by_id[1].torrent_description == "347-350"
    assert by_id[2].hevc_pair_status is None  # AVC с HEVC-парой
    assert by_id[3].hevc_pair_status is None  # сам HEVC


def test_list_release_groups_hevc_filter_empty_when_no_matches() -> None:
    db = MagicMock()
    db.execute.return_value.all.return_value = []

    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)

    assert result["hevc_filter"] == "overdue"
    assert result["groups"] == []
    assert result["total"] == 0
    assert result["total_pages"] == 1
    # без совпадений не ходим в count/stats
    db.scalar.assert_not_called()


def test_list_release_groups_hevc_filter_overdue_marks_status() -> None:
    from datetime import timedelta

    from app.services.hevc_pairing import HEVC_SLA_HOURS
    from app.utils.datetime_fmt import utcnow

    db = MagicMock()
    now = utcnow()
    quality_avc = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "AVC", "value": "x264/AVC"},
    }
    quality_hevc = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "HEVC", "value": "x265/HEVC"},
    }
    # AVC 1-12 + HEVC 1-11 age>SLA → overdue (presence есть), не pure missing
    overdue_row = SimpleNamespace(
        id=1,
        release_id=10,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        torrent_id=100,
        info_hash="a" * 40,
        torrent_type="BDRip 1080p AVC",
        torrent_description="1-12",
        file_size=1024,
        created_at=now - timedelta(hours=HEVC_SLA_HOURS + 2),
        quality_json=quality_avc,
        api_present=True,
        superseded=False,
    )
    hevc_partial = SimpleNamespace(
        id=2,
        release_id=10,
        release_alias="show",
        anime_name="Show",
        category="AniLibria/2024",
        torrent_id=101,
        info_hash="b" * 40,
        torrent_type="BDRip 1080p HEVC",
        torrent_description="1-11",
        file_size=1024,
        created_at=now,
        quality_json=quality_hevc,
        api_present=True,
        superseded=False,
    )
    pairing = [overdue_row, hevc_partial]
    stats_row = SimpleNamespace(
        release_id=10, last_updated=overdue_row.created_at, torrent_count=2
    )
    execute_calls = {"n": 0}

    def _execute(stmt):  # noqa: ANN001
        execute_calls["n"] += 1
        result = MagicMock()
        if execute_calls["n"] == 1:
            result.all.return_value = pairing
        else:
            result.all.return_value = [stats_row]
        return result

    db.execute.side_effect = _execute
    db.scalar.return_value = 1
    db.scalars.side_effect = [
        MagicMock(all=lambda: pairing),  # archives
        MagicMock(all=lambda: []),  # release meta
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: pairing),  # archives for events
    ]

    result = list_release_groups(db, hevc_filter="overdue", page=1, per_page=30)
    assert result["hevc_filter"] == "overdue"
    assert len(result["groups"]) == 1
    by_id = {t.archive_id: t for t in result["groups"][0].torrents}
    t = by_id[1]
    assert t.hevc_pair_status == "overdue"
    assert by_id[2].hevc_pair_status is None
    # Бейдж: часы сверх SLA (age ≈ SLA+2 → ~2ч), не полный age.
    assert t.hevc_pair_age_hours is not None
    assert 1.5 < t.hevc_pair_age_hours < 2.5


def _events_db(rows: list) -> MagicMock:
    db = MagicMock()
    db.scalars.side_effect = [
        MagicMock(all=lambda: rows),
        MagicMock(all=lambda: []),
    ]
    return db


def test_recent_events_keeps_only_latest_kind_per_path() -> None:
    info_hash = "ab" * 20
    # id DESC: сначала added (новый), потом removed (старый)
    rows = [
        SimpleNamespace(
            id=2,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="added",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="removed",
        ),
    ]
    result = _recent_events_by_info_hash(_events_db(rows), [1])
    assert result[info_hash].latest_by_path["ep.mkv"] == "added"


def test_recent_events_collects_removed_candidates() -> None:
    info_hash = "cd" * 20
    rows = [
        SimpleNamespace(
            id=3,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="fille4.mkv",
            full_path="/media/fille4.mkv",
            kind="removed",
        ),
        SimpleNamespace(
            id=2,
            torrent_id=10,
            info_hash=info_hash,
            relative_path=None,
            full_path="/media/orphan.mkv",
            kind="orphan",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="ok.mkv",
            full_path="/media/ok.mkv",
            kind="added",
        ),
    ]
    result = _recent_events_by_info_hash(_events_db(rows), [1])
    assert ("fille4.mkv", "/media/fille4.mkv", "removed") in result[info_hash].removed_candidates
    assert ("/media/orphan.mkv", "/media/orphan.mkv", "orphan") in result[info_hash].removed_candidates
    assert all(c[0] != "ok.mkv" for c in result[info_hash].removed_candidates)


def test_recent_events_skips_removed_superseded_by_added() -> None:
    """После повторного added тот же путь не должен оставаться кандидатом «удалён»."""
    info_hash = "ef" * 20
    rows = [
        SimpleNamespace(
            id=2,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="added",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            info_hash=info_hash,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="removed",
        ),
    ]
    result = _recent_events_by_info_hash(_events_db(rows), [1])
    assert result[info_hash].latest_by_path["ep.mkv"] == "added"
    assert result[info_hash].removed_candidates == []


def test_build_file_rows_checking_only_with_active_hash_job(monkeypatch) -> None:
    def _status(**kwargs):  # noqa: ANN003
        if not kwargs.get("in_torrent", True):
            return "removed"
        if kwargs.get("hash_job_active") and kwargs.get("ui_status") != "new":
            return "checking"
        return kwargs.get("ui_status") or "ok"

    monkeypatch.setattr("app.services.releases_view.file_status_for_ui", _status)
    files = [
        SimpleNamespace(
            id=1,
            relative_path="ep.mkv",
            size=10,
            selected=True,
            full_path="/media/ep.mkv",
            ui_status="ok",
        )
    ]
    disk = {"/media/ep.mkv": SimpleNamespace(content_hash="abc")}

    with_job = _build_file_rows(files, {}, disk, hash_job_active=True)
    assert with_job[0].status == "checking"

    without_job = _build_file_rows(files, {}, disk, hash_job_active=False)
    assert without_job[0].status == "ok"

    rows = _build_file_rows(
        files,
        {},
        disk,
        removed_candidates=[("gone.mkv", "/media/gone.mkv"), ("absent.mkv", "/media/absent.mkv")],
    )
    # Sticky removed: оба кандидата, даже без файла на диске
    assert len(rows) == 3
    by_path = {r.relative_path: r for r in rows}
    assert by_path["gone.mkv"].status == "removed"
    assert by_path["absent.mkv"].status == "removed"
    assert by_path["gone.mkv"].in_torrent is False
    assert by_path["absent.mkv"].in_torrent is False


def test_build_file_rows_sorted_by_filename_desc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.services.releases_view.file_status_for_ui",
        lambda **_k: "ok",
    )
    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    media = tmp_path / "Show"
    media.mkdir()
    for name in ("ep1.mkv", "ep2.mkv", "ep10.mkv", "ep03.mkv"):
        (media / name).write_bytes(b"x")

    files = [
        SimpleNamespace(
            id=1,
            relative_path="Show/ep1.mkv",
            size=1,
            selected=True,
            full_path=str(media / "ep1.mkv"),
            ui_status="ok",
        ),
        SimpleNamespace(
            id=2,
            relative_path="Show/ep10.mkv",
            size=1,
            selected=True,
            full_path=str(media / "ep10.mkv"),
            ui_status="new",
        ),
        SimpleNamespace(
            id=3,
            relative_path="Show/ep2.mkv",
            size=1,
            selected=True,
            full_path=str(media / "ep2.mkv"),
            ui_status="ok",
        ),
        SimpleNamespace(
            id=4,
            relative_path="Show/ep03.mkv",
            size=1,
            selected=True,
            full_path=str(media / "ep03.mkv"),
            ui_status="ok",
        ),
    ]
    rows = _build_file_rows(files, {})
    # natural desc: 10 > 3 > 2 > 1 (не лексикографически ep2 > ep10)
    assert [Path(r.relative_path).name for r in rows] == [
        "ep10.mkv",
        "ep03.mkv",
        "ep2.mkv",
        "ep1.mkv",
    ]
    # Без media_present кнопки не рисуем на SSR.
    assert rows[0].downloadable is False
    assert rows[0].file_id == 2


def test_build_file_rows_downloadable_from_media_present(tmp_path: Path) -> None:
    from app.services.releases_view import _build_file_rows

    files = [
        SimpleNamespace(
            id=1,
            relative_path="Show/ep1.mkv",
            size=1,
            selected=True,
            full_path=str(tmp_path / "ep1.mkv"),
            ui_status="ok",
            is_checking=False,
            media_present=True,
        ),
        SimpleNamespace(
            id=2,
            relative_path="Show/ep2.mkv",
            size=1,
            selected=True,
            full_path=str(tmp_path / "ep2.mkv"),
            ui_status="ok",
            is_checking=True,
            media_present=True,
        ),
        SimpleNamespace(
            id=3,
            relative_path="Show/ep3.mkv",
            size=1,
            selected=True,
            full_path=str(tmp_path / "ep3.mkv"),
            ui_status="ok",
            is_checking=False,
            media_present=False,
        ),
    ]
    rows = {r.file_id: r for r in _build_file_rows(files, {})}
    assert rows[1].downloadable is True
    assert rows[1].status == "ok"
    assert rows[2].downloadable is False
    assert rows[2].status == "checking"
    assert rows[3].downloadable is False


def test_list_downloadable_file_ids_only_active(tmp_path: Path, monkeypatch) -> None:
    from app.services.releases_view import list_downloadable_file_ids

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.services.releases_view._info_hashes_with_active_hash_job",
        lambda _db, _hashes: set(),
    )
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"data")
    info_hash = "ab" * 20

    archive = SimpleNamespace(info_hash=info_hash, superseded=False, api_present=True)
    tf = SimpleNamespace(
        id=7,
        info_hash=info_hash,
        ui_status="ok",
        full_path=str(media),
        relative_path="ep.mkv",
        is_checking=False,
        media_present=True,
    )
    db = MagicMock()
    db.scalar.return_value = archive
    db.scalars.return_value.all.return_value = [tf]
    assert list_downloadable_file_ids(db, info_hash) == [7]

    archive.superseded = True
    assert list_downloadable_file_ids(db, info_hash) == []

    archive.superseded = False
    archive.api_present = False
    assert list_downloadable_file_ids(db, info_hash) == []


def test_probe_torrent_media_files_checking_and_downloadable(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.releases_view import probe_torrent_media_files

    monkeypatch.setattr(
        "app.services.releases_view._info_hashes_with_active_hash_job",
        lambda _db, _hashes: set(),
    )
    info_hash = "cd" * 20

    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        info_hash=info_hash, superseded=False, api_present=True
    )
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(
            id=1,
            ui_status="ok",
            full_path=str(tmp_path / "ep01.mkv"),
            relative_path="ep01.mkv",
            is_checking=True,
            media_present=False,
        ),
        SimpleNamespace(
            id=2,
            ui_status="ok",
            full_path=str(tmp_path / "ep02.mkv"),
            relative_path="ep02.mkv",
            is_checking=False,
            media_present=True,
        ),
        SimpleNamespace(
            id=3,
            ui_status="new",
            full_path=str(tmp_path / "ep03.mkv"),
            relative_path="ep03.mkv",
            is_checking=True,
            media_present=False,
        ),
    ]
    probe = probe_torrent_media_files(db, info_hash)
    assert probe.downloadable_ids == [2]
    assert probe.checking_ids == [1]


def test_probe_checking_from_master_progress(tmp_path: Path, monkeypatch) -> None:
    """refresh=True: progress<1 на master → checking_ids."""
    from app.services.releases_view import probe_torrent_media_files

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.services.releases_view._info_hashes_with_active_hash_job",
        lambda _db, _hashes: set(),
    )
    monkeypatch.setattr(
        "app.services.qb_inventory.fetch_master_file_progress",
        lambda _db, _h: {0: 0.4, 1: 1.0},
    )
    show = tmp_path / "Show"
    show.mkdir()
    ep05 = show / "ep05.mkv"
    ep05.write_bytes(b"old")
    ep04 = show / "ep04.mkv"
    ep04.write_bytes(b"ok")
    info_hash = "ab" * 20

    rows = [
        SimpleNamespace(
            id=1,
            ui_status="ok",
            full_path=str(ep05),
            relative_path="ep05.mkv",
            is_checking=False,
            media_present=True,
            file_index=0,
            selected=True,
        ),
        SimpleNamespace(
            id=2,
            ui_status="ok",
            full_path=str(ep04),
            relative_path="ep04.mkv",
            is_checking=False,
            media_present=True,
            file_index=1,
            selected=True,
        ),
    ]
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        info_hash=info_hash, superseded=False, api_present=True
    )
    db.scalars.return_value.all.return_value = rows
    probe = probe_torrent_media_files(db, info_hash, refresh=True)
    assert probe.checking_ids == [1]
    assert 2 not in probe.checking_ids
    assert probe.downloadable_ids == [2]


def test_probe_checking_from_db_not_disk_partial(tmp_path: Path, monkeypatch) -> None:
    """Без refresh .!qB на диске не влияет; флаг is_checking в БД — даёт checking."""
    from app.services.releases_view import probe_torrent_media_files

    monkeypatch.setattr(
        "app.services.releases_view._info_hashes_with_active_hash_job",
        lambda _db, _hashes: set(),
    )
    show = tmp_path / "Show"
    show.mkdir()
    disk_partial = show / "ep01.mkv"
    Path(str(disk_partial) + ".!qB").write_bytes(b"part")
    complete = show / "ep02.mkv"
    complete.write_bytes(b"ok")
    info_hash = "ee" * 20
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        info_hash=info_hash, superseded=False, api_present=True
    )
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(
            id=1,
            ui_status="ok",
            full_path=str(disk_partial),
            relative_path="ep01.mkv",
            is_checking=False,
            media_present=False,
        ),
        SimpleNamespace(
            id=2,
            ui_status="changed",
            full_path=str(complete),
            relative_path="ep02.mkv",
            is_checking=True,
            media_present=True,
        ),
    ]
    probe = probe_torrent_media_files(db, info_hash)
    assert probe.checking_ids == [2]
    assert 1 not in probe.checking_ids
    # checking overlay → не downloadable
    assert probe.downloadable_ids == []


def test_probe_torrents_media_files_batch_db_only(monkeypatch) -> None:
    from app.services.releases_view import probe_torrents_media_files

    monkeypatch.setattr(
        "app.services.releases_view._info_hashes_with_active_hash_job",
        lambda _db, _hashes: set(),
    )
    h1 = "aa" * 20
    h2 = "bb" * 20
    archives = [
        SimpleNamespace(info_hash=h1, superseded=False, api_present=True),
        SimpleNamespace(info_hash=h2, superseded=False, api_present=True),
    ]
    files = [
        SimpleNamespace(
            id=1,
            info_hash=h1,
            ui_status="ok",
            full_path="/m/a.mkv",
            relative_path="a.mkv",
            is_checking=False,
            media_present=True,
        ),
        SimpleNamespace(
            id=2,
            info_hash=h2,
            ui_status="ok",
            full_path="/m/b.mkv",
            relative_path="b.mkv",
            is_checking=True,
            media_present=True,
        ),
    ]
    db = MagicMock()
    db.scalars.side_effect = [
        MagicMock(all=MagicMock(return_value=archives)),
        MagicMock(all=MagicMock(return_value=files)),
    ]
    scan = MagicMock()
    monkeypatch.setattr("app.services.releases_view._scan_media_presence", scan)
    out = probe_torrents_media_files(db, [h1, h2], refresh=False)
    scan.assert_not_called()
    assert out[h1].downloadable_ids == [1]
    assert out[h1].checking_ids == []
    assert out[h2].downloadable_ids == []
    assert out[h2].checking_ids == [2]


def test_natural_name_key_orders_unpadded() -> None:
    from app.services.releases_view import _natural_name_key

    names = ["ep10.mkv", "ep2.mkv", "ep1.mkv", "ep03.mkv"]
    assert sorted(names, key=_natural_name_key) == [
        "ep1.mkv",
        "ep2.mkv",
        "ep03.mkv",
        "ep10.mkv",
    ]


def test_build_file_rows_ssr_skips_disk_partial_overlay(tmp_path: Path) -> None:
    """SSR не смотрит .!qB — sticky ok остаётся ok без is_checking в БД."""
    from app.services.releases_view import _build_file_rows

    show = tmp_path / "Show"
    show.mkdir()
    partial = show / "ep01.mkv"
    Path(str(partial) + ".!qB").write_bytes(b"part")

    files = [
        SimpleNamespace(
            id=1,
            relative_path="Show/ep01.mkv",
            size=1,
            selected=True,
            full_path=str(partial),
            ui_status="ok",
            is_checking=False,
        ),
    ]
    rows = _build_file_rows(files, {})
    assert rows[0].status == "ok"


def test_build_file_rows_checking_from_db_flag() -> None:
    from app.services.releases_view import _build_file_rows

    files = [
        SimpleNamespace(
            id=1,
            relative_path="Show/ep01.mkv",
            size=1,
            selected=True,
            full_path="/media/Show/ep01.mkv",
            ui_status="ok",
            is_checking=True,
        ),
        SimpleNamespace(
            id=2,
            relative_path="Show/ep02.mkv",
            size=1,
            selected=True,
            full_path="/media/Show/ep02.mkv",
            ui_status="changed",
            is_checking=True,
        ),
        SimpleNamespace(
            id=3,
            relative_path="Show/ep03.mkv",
            size=1,
            selected=True,
            full_path="/media/Show/ep03.mkv",
            ui_status="new",
            is_checking=True,
        ),
    ]
    rows = {r.relative_path: r for r in _build_file_rows(files, {})}
    assert rows["Show/ep01.mkv"].status == "checking"
    assert rows["Show/ep02.mkv"].status == "checking"
    assert rows["Show/ep03.mkv"].status == "new"


def test_build_file_rows_checking_cleared_shows_sticky() -> None:
    from app.services.releases_view import _build_file_rows

    files = [
        SimpleNamespace(
            id=1,
            relative_path="Show/ep07.mkv",
            size=1,
            selected=True,
            full_path="/media/Show/ep07.mkv",
            ui_status="changed",
            is_checking=False,
        ),
    ]
    rows = _build_file_rows(files, {}, hash_job_active=False)
    assert rows[0].status == "changed"


def test_file_is_downloadable_rules(tmp_path: Path, monkeypatch) -> None:
    from app.services.releases_view import _file_is_downloadable

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    media = tmp_path / "a.mkv"
    media.write_bytes(b"ok")
    incomplete = Path(str(media) + ".!qB")
    incomplete.write_bytes(b"part")

    assert _file_is_downloadable(status="ok", full_path=str(media)) is True
    assert _file_is_downloadable(status="new", full_path=str(media)) is True
    assert _file_is_downloadable(status="changed", full_path=str(media)) is True
    assert _file_is_downloadable(status="checking", full_path=str(media)) is False
    assert _file_is_downloadable(status="removed", full_path=str(media)) is False
    assert _file_is_downloadable(status="ok", full_path=str(incomplete)) is False
    assert _file_is_downloadable(status="ok", full_path=None) is False
    assert _file_is_downloadable(status="ok", full_path=str(tmp_path / "missing.mkv")) is False
    assert _file_is_downloadable(status="ok", full_path="/etc/passwd") is False
    # батч: только то, что в existing_resolved
    existing = {str(media.resolve())}
    assert (
        _file_is_downloadable(
            status="ok", full_path=str(media), existing_resolved=existing
        )
        is True
    )
    assert (
        _file_is_downloadable(
            status="ok",
            full_path=str(media),
            existing_resolved=set(),
        )
        is False
    )


def test_existing_resolved_files_batches_by_parent(tmp_path: Path, monkeypatch) -> None:
    from app.services.releases_view import _existing_resolved_files

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    show = tmp_path / "Show"
    show.mkdir()
    a = show / "a.mkv"
    b = show / "b.mkv"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    missing = show / "c.mkv"
    found = _existing_resolved_files(
        [str(a), str(b), str(missing), "/etc/passwd"],
        media_root=tmp_path.resolve(),
    )
    assert str(a.resolve()) in found
    assert str(b.resolve()) in found
    assert str(missing.resolve()) not in found


def test_resolve_media_file_for_download(tmp_path: Path, monkeypatch) -> None:
    from app.services.releases_view import resolve_media_file_for_download

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"data")
    row = SimpleNamespace(ui_status="new", full_path=str(media))
    assert resolve_media_file_for_download(row) == media.resolve()

    row.ui_status = "checking"
    assert resolve_media_file_for_download(row) is None

    row.ui_status = "ok"
    row.full_path = None
    assert resolve_media_file_for_download(row) is None


def test_download_torrent_media_file_endpoint(tmp_path: Path, monkeypatch) -> None:
    """HTTP-хендлер: 200 для ok на диске; 404 вне root / .!qB / checking / removed / missing / архив."""
    from fastapi import HTTPException

    from app.api.rest import download_torrent_media_file

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    incomplete = Path(str(media) + ".!qB")
    incomplete.write_bytes(b"part")
    info_hash = "aa" * 20

    def _call(row, *, allow: bool = True):
        monkeypatch.setattr(
            "app.api.rest.torrent_allows_media_download",
            lambda _db, _h: allow,
        )
        db = MagicMock()
        db.get.return_value = row
        return download_torrent_media_file(1, db=db)

    ok_row = SimpleNamespace(ui_status="ok", full_path=str(media), info_hash=info_hash)
    resp = _call(ok_row)
    assert Path(resp.path) == media.resolve()
    assert resp.filename == "ep.mkv"

    db_miss = MagicMock()
    db_miss.get.return_value = None
    try:
        download_torrent_media_file(99, db=db_miss)
        assert False, "expected 404"
    except HTTPException as exc:
        assert exc.status_code == 404

    # Неактуальный / superseded: gate закрыт
    try:
        _call(ok_row, allow=False)
        assert False, "expected 404 when archive not active"
    except HTTPException as exc:
        assert exc.status_code == 404

    for bad in (
        SimpleNamespace(ui_status="ok", full_path="/etc/passwd", info_hash=info_hash),
        SimpleNamespace(ui_status="ok", full_path=str(incomplete), info_hash=info_hash),
        SimpleNamespace(ui_status="checking", full_path=str(media), info_hash=info_hash),
        SimpleNamespace(ui_status="removed", full_path=str(media), info_hash=info_hash),
        SimpleNamespace(ui_status="ok", full_path=str(tmp_path / "nope.mkv"), info_hash=info_hash),
        SimpleNamespace(ui_status="new", full_path=None, info_hash=info_hash),
    ):
        try:
            _call(bad)
            assert False, f"expected 404 for {bad!r}"
        except HTTPException as exc:
            assert exc.status_code == 404


def test_download_rejects_superseded_via_archive_gate(tmp_path: Path, monkeypatch) -> None:
    """End-to-end gate: superseded archive → 404 без мока torrent_allows."""
    from fastapi import HTTPException

    from app.api.rest import download_torrent_media_file

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"payload")
    info_hash = "ee" * 20
    row = SimpleNamespace(id=1, ui_status="ok", full_path=str(media), info_hash=info_hash)

    db = MagicMock()
    db.get.return_value = row
    db.scalar.return_value = SimpleNamespace(
        info_hash=info_hash, superseded=True, api_present=True
    )
    try:
        download_torrent_media_file(1, db=db)
        assert False, "expected 404"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_list_torrent_downloadable_files_endpoint(tmp_path: Path, monkeypatch) -> None:
    from app.api.rest import list_torrent_downloadable_files
    from app.services.releases_view import TorrentMediaProbe

    monkeypatch.setattr(
        "app.api.rest.probe_torrent_media_files",
        lambda _db, h, refresh=False: TorrentMediaProbe(
            downloadable_ids=[2, 5], checking_ids=[1]
        ),
    )
    db = MagicMock()
    out = list_torrent_downloadable_files("ab" * 20, db=db)
    assert out["file_ids"] == [2, 5]
    assert out["checking_ids"] == [1]
    assert out["info_hash"] == "ab" * 20


def test_list_torrents_downloadable_files_batch_endpoint(monkeypatch) -> None:
    from app.api.rest import (
        DownloadableFilesBatchIn,
        list_torrents_downloadable_files_batch,
    )
    from app.services.releases_view import TorrentMediaProbe
    from fastapi import HTTPException

    h1 = "aa" * 20
    h2 = "bb" * 20

    def fake_batch(_db, hashes, refresh=False):
        assert refresh is False
        return {
            h1: TorrentMediaProbe(downloadable_ids=[1], checking_ids=[]),
            h2: TorrentMediaProbe(downloadable_ids=[], checking_ids=[9]),
        }

    monkeypatch.setattr("app.api.rest.probe_torrents_media_files", fake_batch)
    out = list_torrents_downloadable_files_batch(
        DownloadableFilesBatchIn(hashes=[h1, h2]), db=MagicMock()
    )
    assert out["items"] == [
        {"info_hash": h1, "file_ids": [1], "checking_ids": []},
        {"info_hash": h2, "file_ids": [], "checking_ids": [9]},
    ]

    try:
        list_torrents_downloadable_files_batch(
            DownloadableFilesBatchIn(hashes=[f"{i:040x}" for i in range(101)]),
            db=MagicMock(),
        )
        assert False, "expected 400"
    except HTTPException as exc:
        assert exc.status_code == 400


def test_list_torrents_downloadable_files_batch_refresh_flag(monkeypatch) -> None:
    """refresh из тела запроса прокидывается в probe (клиент шлёт отдельно refresh/db-only)."""
    from app.api.rest import (
        DownloadableFilesBatchIn,
        list_torrents_downloadable_files_batch,
    )
    from app.services.releases_view import TorrentMediaProbe

    h1 = "aa" * 20
    seen: list[bool] = []

    def fake_batch(_db, hashes, refresh=False):
        seen.append(bool(refresh))
        return {h1: TorrentMediaProbe(downloadable_ids=[1], checking_ids=[])}

    monkeypatch.setattr("app.api.rest.probe_torrents_media_files", fake_batch)
    list_torrents_downloadable_files_batch(
        DownloadableFilesBatchIn(hashes=[h1], refresh=True), db=MagicMock()
    )
    list_torrents_downloadable_files_batch(
        DownloadableFilesBatchIn(hashes=[h1], refresh=False), db=MagicMock()
    )
    assert seen == [True, False]


def test_refresh_media_flags_clears_is_checking_when_master_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Master недоступен: path reconcile снимает stuck is_checking при complete на диске."""
    from app.services.releases_view import _refresh_media_flags_from_disk

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.services.qb_inventory.refresh_checking_flags_from_master",
        lambda _db, _h: None,
    )
    show = tmp_path / "Show"
    show.mkdir()
    ep = show / "ep01.mkv"
    ep.write_bytes(b"complete")
    info_hash = "ff" * 20
    row = SimpleNamespace(
        id=1,
        ui_status="ok",
        full_path=str(ep),
        relative_path="ep01.mkv",
        is_checking=True,
        media_present=False,
    )
    db = MagicMock()
    _refresh_media_flags_from_disk(db, [row], info_hash=info_hash)
    assert row.is_checking is False
    assert row.media_present is True
    db.commit.assert_called()


def test_refresh_media_flags_path_sets_checking_when_master_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Master недоступен: .!qB без complete → is_checking true."""
    from app.services.releases_view import _refresh_media_flags_from_disk

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.services.qb_inventory.refresh_checking_flags_from_master",
        lambda _db, _h: None,
    )
    show = tmp_path / "Show"
    show.mkdir()
    ep = show / "ep01.mkv"
    Path(str(ep) + ".!qB").write_bytes(b"part")
    info_hash = "fe" * 20
    row = SimpleNamespace(
        id=1,
        ui_status="ok",
        full_path=str(ep),
        relative_path="ep01.mkv",
        is_checking=False,
        media_present=True,
    )
    db = MagicMock()
    _refresh_media_flags_from_disk(db, [row], info_hash=info_hash)
    assert row.is_checking is True
    assert row.media_present is False


def test_refresh_media_flags_skips_path_when_master_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """Master ответил — path-fallback не затирает is_checking от progress."""
    from app.services.releases_view import _refresh_media_flags_from_disk

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.services.qb_inventory.refresh_checking_flags_from_master",
        lambda _db, _h: False,
    )
    show = tmp_path / "Show"
    show.mkdir()
    ep = show / "ep01.mkv"
    ep.write_bytes(b"growing")
    info_hash = "fd" * 20
    row = SimpleNamespace(
        id=1,
        ui_status="ok",
        full_path=str(ep),
        relative_path="ep01.mkv",
        is_checking=True,
        media_present=True,
    )
    db = MagicMock()
    _refresh_media_flags_from_disk(db, [row], info_hash=info_hash)
    assert row.is_checking is True


def test_filter_removed_candidates_drops_foreign_titles(tmp_path, monkeypatch) -> None:
    from app.services.releases_view import _filter_removed_candidates

    media = tmp_path / "anilibria"
    show = media / "2012" / "Sakurasou"
    show.mkdir(parents=True)
    ep = show / "ep01.mkv"
    ep.write_bytes(b"1")
    foreign = media / "2012" / "Nekomonogatari" / "bonus.mkv"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"x")

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.releases_view.resolve_orphan_scan_root",
        lambda **kwargs: show.resolve(),
    )

    files = [
        SimpleNamespace(relative_path="ep01.mkv", full_path=str(ep.resolve())),
    ]
    filtered = _filter_removed_candidates(
        [
            ("ep01.mkv", str(ep.resolve())),
            ("bonus.mkv", str(foreign.resolve())),
            ("/anilibria/2012/other.mkv", "/anilibria/2012/other.mkv"),
        ],
        files,  # type: ignore[arg-type]
    )
    paths = {full for _display, full, _kind in filtered}
    assert str(ep.resolve()) in paths
    assert str(foreign.resolve()) not in paths


def test_filter_removed_candidates_drops_ds_store(tmp_path, monkeypatch) -> None:
    from app.services.releases_view import _filter_removed_candidates

    media = tmp_path / "anilibria"
    show = media / "2009" / "Hetalia"
    show.mkdir(parents=True)
    ep = show / "ep01.mkv"
    ep.write_bytes(b"1")
    ds = show / ".DS_Store"
    ds.write_bytes(b"junk")

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.releases_view.resolve_orphan_scan_root",
        lambda **kwargs: show.resolve(),
    )
    files = [SimpleNamespace(relative_path="ep01.mkv", full_path=str(ep))]
    filtered = _filter_removed_candidates(
        [
            ("ep01.mkv", str(ep)),
            (str(ds), str(ds)),
            (".DS_Store", str(ds)),
        ],
        files,  # type: ignore[arg-type]
    )
    assert all(not str(p).endswith(".DS_Store") for _, p, *_rest in filtered if p)


def test_info_hashes_with_active_hash_job() -> None:
    db = MagicMock()
    wanted = "aa" * 20
    other = "bb" * 20
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(params_json={"info_hash": wanted.upper()}),
        SimpleNamespace(params_json={"info_hash": other}),
        SimpleNamespace(params_json={}),
    ]
    active = _info_hashes_with_active_hash_job(db, [wanted])
    assert active == {wanted}


def test_latest_pipeline_by_hash_prefers_non_failed() -> None:
    from app.services.releases_view import _latest_pipeline_by_hash

    h = "ab" * 20
    db = MagicMock()
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(info_hash=h, status="failed", error="boom", id=30),
        SimpleNamespace(info_hash=h, status="done", error=None, id=20),
        SimpleNamespace(info_hash=h, status="cancelled", error="x", id=10),
    ]
    result = _latest_pipeline_by_hash(db, [h])
    assert result[h] == ("done", None, 20)


def test_latest_pipeline_by_hash_falls_back_to_failed() -> None:
    from app.services.releases_view import _latest_pipeline_by_hash

    h = "cd" * 20
    db = MagicMock()
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(info_hash=h, status="failed", error="boom", id=5),
        SimpleNamespace(info_hash=h, status="cancelled", error="x", id=4),
    ]
    result = _latest_pipeline_by_hash(db, [h])
    assert result[h] == ("failed", "boom", 5)


def test_format_torrent_files_summary_mixed_with_removed() -> None:
    files = [
        SimpleNamespace(status="removed"),
        SimpleNamespace(status="new"),
        SimpleNamespace(status="ok"),
        SimpleNamespace(status="ok"),
        SimpleNamespace(status="ok"),
    ]
    assert (
        format_torrent_files_summary(files)
        == "4 файла (1 новый, 3 старые), 1 удалён."
    )


def test_format_torrent_files_summary_user_example_new_changed_ok_removed() -> None:
    files = [
        SimpleNamespace(status="new"),
        SimpleNamespace(status="changed"),
        SimpleNamespace(status="ok"),
        SimpleNamespace(status="ok"),
        SimpleNamespace(status="removed"),
    ]
    assert (
        format_torrent_files_summary(files)
        == "4 файла (1 новый, 1 изменён, 2 старые), 1 удалён."
    )


def test_format_torrent_files_summary_all_ok_plain() -> None:
    files = [SimpleNamespace(status="ok") for _ in range(4)]
    assert format_torrent_files_summary(files) == "4 файла."


def test_format_torrent_files_summary_only_removed() -> None:
    files = [SimpleNamespace(status="removed"), SimpleNamespace(status="removed")]
    assert format_torrent_files_summary(files) == "2 удалённых."


def test_format_torrent_files_summary_plural_forms() -> None:
    assert format_torrent_files_summary([SimpleNamespace(status="ok")]) == "1 файл."
    assert (
        format_torrent_files_summary(
            [SimpleNamespace(status="new") for _ in range(5)]
        )
        == "5 файлов (5 новых)."
    )
    assert (
        format_torrent_files_summary(
            [SimpleNamespace(status="ok") for _ in range(3)]
            + [SimpleNamespace(status="removed")]
        )
        == "3 файла (3 старые), 1 удалён."
    )
    assert (
        format_torrent_files_summary(
            [SimpleNamespace(status="ok") for _ in range(5)]
            + [SimpleNamespace(status="removed")]
        )
        == "5 файлов (5 старых), 1 удалён."
    )
