from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.releases_view import (
    _build_file_rows,
    _info_hashes_with_active_hash_job,
    _recent_events_by_torrent,
    format_bytes,
    list_release_groups,
)


def test_format_bytes() -> None:
    assert format_bytes(None) == "-"
    assert format_bytes(500) == "500 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


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
    )
    db.scalars.side_effect = [
        MagicMock(all=lambda: [archive]),  # archives
        MagicMock(all=lambda: []),  # pipelines
        MagicMock(all=lambda: []),  # tracked
        MagicMock(all=lambda: []),  # torrent_files
        MagicMock(all=lambda: []),  # events
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


def test_recent_events_keeps_only_latest_kind_per_path() -> None:
    db = MagicMock()
    # id DESC: сначала added (новый), потом removed (старый)
    rows = [
        SimpleNamespace(
            id=2,
            torrent_id=10,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="added",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="removed",
        ),
    ]
    db.scalars.return_value.all.return_value = rows

    result = _recent_events_by_torrent(db, [1])
    assert result[10].latest_by_path["ep.mkv"] == "added"


def test_recent_events_collects_removed_candidates() -> None:
    db = MagicMock()
    rows = [
        SimpleNamespace(
            id=3,
            torrent_id=10,
            relative_path="fille4.mkv",
            full_path="/media/fille4.mkv",
            kind="removed",
        ),
        SimpleNamespace(
            id=2,
            torrent_id=10,
            relative_path=None,
            full_path="/media/orphan.mkv",
            kind="orphan",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            relative_path="ok.mkv",
            full_path="/media/ok.mkv",
            kind="added",
        ),
    ]
    db.scalars.return_value.all.return_value = rows
    result = _recent_events_by_torrent(db, [1])
    assert ("fille4.mkv", "/media/fille4.mkv") in result[10].removed_candidates
    assert ("/media/orphan.mkv", "/media/orphan.mkv") in result[10].removed_candidates
    assert all(c[0] != "ok.mkv" for c in result[10].removed_candidates)


def test_recent_events_skips_removed_superseded_by_added() -> None:
    """После повторного added тот же путь не должен оставаться кандидатом «удалён»."""
    db = MagicMock()
    rows = [
        SimpleNamespace(
            id=2,
            torrent_id=10,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="added",
        ),
        SimpleNamespace(
            id=1,
            torrent_id=10,
            relative_path="ep.mkv",
            full_path="/media/ep.mkv",
            kind="removed",
        ),
    ]
    db.scalars.return_value.all.return_value = rows
    result = _recent_events_by_torrent(db, [1])
    assert result[10].latest_by_path["ep.mkv"] == "added"
    assert result[10].removed_candidates == []


def test_build_file_rows_checking_only_with_active_hash_job(monkeypatch) -> None:
    def _status(**kwargs):  # noqa: ANN003
        if not kwargs.get("in_torrent", True):
            return "removed"
        if kwargs.get("hash_job_active") and kwargs.get("disk_hash"):
            return "checking"
        if kwargs.get("latest_kind") == "modified":
            return "changed"
        return "ok"

    monkeypatch.setattr("app.services.releases_view.file_status_for_ui", _status)
    monkeypatch.setattr(
        "app.services.releases_view.path_exists_including_incomplete",
        lambda path: str(path) == "/media/gone.mkv",
    )
    files = [
        SimpleNamespace(
            relative_path="ep.mkv",
            size=10,
            selected=True,
            full_path="/media/ep.mkv",
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
    assert len(rows) == 2
    assert rows[1].status == "removed"
    assert rows[1].relative_path == "gone.mkv"
    assert rows[1].in_torrent is False


def test_info_hashes_with_active_hash_job_filters_wanted() -> None:
    info = "ab" * 20
    db = MagicMock()
    # SQL уже ограничивает pending/running; mock возвращает активные jobs
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(params_json={"info_hash": info}),
        SimpleNamespace(params_json={"info_hash": "cd" * 20}),
    ]
    active = _info_hashes_with_active_hash_job(db, [info, "zz" * 20])
    assert active == {info}