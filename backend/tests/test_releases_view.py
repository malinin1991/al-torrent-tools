from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.releases_view import (
    _build_file_rows,
    _info_hashes_with_active_hash_job,
    _recent_events_by_info_hash,
    format_bytes,
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
        quality_json={"genres": ["Комедия", "Романтика"]},
        api_present=True,
        superseded=False,
    )
    db.scalars.side_effect = [
        MagicMock(all=lambda: [archive]),  # archives
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
        MagicMock(all=lambda: [archive]),  # archives for events legacy map
    ]

    result = list_release_groups(db, page=1, per_page=30)

    assert len(result["groups"]) == 1
    assert result["groups"][0].genres == ["Комедия", "Романтика"]
    assert result["groups"][0].tracked is False
    assert result["groups"][0].track_source is None
    assert result["groups"][0].torrents[0].api_present is True
    assert result["groups"][0].archived_torrents == []
    assert result["tracked_only"] is False


def test_list_release_groups_tracked_only_flag() -> None:
    db = MagicMock()
    db.scalar.return_value = 0
    db.execute.return_value.all.return_value = []

    result = list_release_groups(db, tracked_only=True, page=1, per_page=30)

    assert result["tracked_only"] is True
    assert result["groups"] == []
    assert result["total"] == 0
    # count-запрос должен ограничивать enabled tracked_releases
    count_stmt = db.scalar.call_args[0][0]
    sql = str(count_stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    assert "tracked_releases" in sql
    assert "enabled" in sql


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
    assert ("fille4.mkv", "/media/fille4.mkv") in result[info_hash].removed_candidates
    assert ("/media/orphan.mkv", "/media/orphan.mkv") in result[info_hash].removed_candidates
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
    assert {r.relative_path for r in rows[1:]} == {"gone.mkv", "absent.mkv"}
    assert all(r.status == "removed" for r in rows[1:])
    assert all(r.in_torrent is False for r in rows[1:])


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
    paths = {full for _display, full in filtered}
    assert str(ep.resolve()) in paths
    assert str(foreign.resolve()) not in paths


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
