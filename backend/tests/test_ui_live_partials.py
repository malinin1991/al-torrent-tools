"""Smoke: live-partials и pipeline detail live."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.templating import Jinja2Templates

from app.services.releases_view import format_torrent_files_summary
from app.utils.datetime_fmt import as_utc_iso

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["as_utc_iso"] = as_utc_iso
    templates.env.filters["torrent_files_summary"] = format_torrent_files_summary
    from app.services.pipeline import pipeline_ci_stages

    templates.env.globals["pipeline_ci_stages"] = pipeline_ci_stages
    return templates


def test_pipeline_detail_live_partial_renders() -> None:
    templates = _templates()
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    pipeline = SimpleNamespace(
        id=3,
        status="done",
        tg_status="skipped",
        release_id=1,
        torrent_id=2,
        info_hash="ab" * 20,
        master_added_at=None,
        slave_added_at=None,
        slave_completed_at=now,
        created_at=now,
        error=None,
    )
    event = SimpleNamespace(
        id=9,
        created_at=now,
        event_type="slave_add",
        from_status="master_complete",
        to_status="slave_added",
        message="на slave",
        job_id=7,
        details_json={"actor": "poll"},
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "partials/pipeline_detail_live.html",
        {
            "request": request,
            "pipeline": pipeline,
            "release_name": "Show",
            "torrent_label": "BDRip · 1-2",
            "master_state": {},
            "slave_state": {},
            "files_status": "success",
            "tracked": True,
            "life_path_text": "path",
            "timeline": [
                {
                    "event": event,
                    "job_exists": True,
                    "job_link": "/jobs?job_id=7",
                    "actor": "poll",
                    "details_pretty": '{\n  "actor": "poll"\n}',
                }
            ],
        },
    ).body.decode("utf-8")
    assert "Жизненный путь" in html
    assert "slave_add" in html
    assert 'data-sse-key="pe-9"' in html
    assert "gl-pipeline" in html
    assert "На slave" in html


def test_releases_live_partial_has_sse_keys() -> None:
    templates = _templates()
    group = SimpleNamespace(
        release_id=42,
        anime_name="Show",
        release_alias="show",
        tracked=False,
        track_source=None,
        is_blocked_by_geo=False,
        is_blocked_by_copyrights=False,
        genres=[],
        members=[],
        torrents=[
            SimpleNamespace(
                archive_id=7,
                torrent_id=100,
                info_hash="ab" * 20,
                torrent_type="WEBRip",
                torrent_description="1-12",
                file_size_label="1.0 GB",
                created_at=None,
                api_created_at=None,
                pipeline_status=None,
                pipeline_error=None,
                pipeline_id=None,
                files_summary="2 файла.",
                ignore_hevc=False,
                hevc_pair_status=None,
                codec_family=None,
            )
        ],
        archived_torrents=[],
        category=None,
        last_updated=None,
        release_url=None,
        admin_url=None,
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "partials/releases_live.html",
        {
            "request": request,
            "groups": [group],
            "search": "",
            "tracked_only": False,
            "hevc_filter": "",
            "show_hidden": False,
            "page": 1,
            "total": 1,
            "total_pages": 1,
        },
    ).body.decode("utf-8")
    assert 'data-sse-key="release-42"' in html
    assert "live" in html
    assert "Принудительно обновить релиз" in html
    assert 'hx-post="/actions/run/force_release_sync"' in html
    assert '"release": "42"' in html or '"release":"42"' in html
    assert "torrent_file_list_lazy" not in html  # имя файла не в разметке
    assert 'hx-get="/archive/7/files' in html
    assert "toggle once from:closest details" in html
    assert "2 файла." in html
    assert 'class="file-list"' not in html  # полный список — lazy
    assert "Выбрать все" not in html  # select-all внутри загруженного body, не в summary


def test_releases_live_url_uses_urlencode() -> None:
    text = (_TEMPLATES_DIR / "releases.html").read_text(encoding="utf-8")
    assert "search|urlencode" in text
    assert 'id="releases-action-result"' in text
    base = (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "_uiSsePreserveByTarget" in base
    assert "lazyBodies" in base
    assert "encodeChecked" in base
    assert "Вложения" in base
    assert "Шрифты" not in base
    assert "visibilitychange" in base
    assert "setTimeout(() => _uiSseRefreshing.delete(key), 3000)" in base
    from app.services import ui_events

    assert ui_events._POLL_INTERVAL_SEC == 3.0


def test_archive_live_partial_marker() -> None:
    templates = _templates()
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "partials/archive_live.html",
        {
            "request": request,
            "rows": [],
            "search": "",
            "page": 1,
            "total": 0,
            "total_pages": 1,
        },
    ).body.decode("utf-8")
    assert "live" in html
    assert "Записей пока нет" in html


def test_pipeline_detail_live_route(monkeypatch) -> None:
    from app.main import pipeline_detail_live

    pipeline = SimpleNamespace(
        id=15,
        status="done",
        info_hash="ab" * 20,
        release_id=10,
        torrent_id=55,
        master_added_at=None,
        slave_added_at=None,
        slave_completed_at=None,
        error=None,
        tg_status="skipped",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    db = MagicMock()
    db.get.return_value = pipeline
    db.scalars.side_effect = [
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: []),
    ]
    db.scalar.return_value = SimpleNamespace(
        anime_name="Show",
        release_alias="show",
        torrent_type="BDRip",
        torrent_description="1",
    )

    async def _fake_to_thread(fn, *a, **k):  # noqa: ANN001
        return {}

    monkeypatch.setattr("app.main.asyncio.to_thread", _fake_to_thread)
    captured: dict = {}

    def _tpl(request, name, ctx):  # noqa: ANN001
        captured["name"] = name
        captured["ctx"] = ctx
        return ctx

    monkeypatch.setattr("app.main.templates.TemplateResponse", _tpl)
    asyncio.run(pipeline_detail_live(MagicMock(), 15, db=db))
    assert captured["name"] == "partials/pipeline_detail_live.html"
    assert captured["ctx"]["pipeline"].id == 15


def test_base_html_has_no_interval_poll_hint_in_sse_script() -> None:
    text = (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "initUiSse" in text
    assert "EventSource" in text
    assert "show:none" in text


def test_jobs_page_no_hx_every_trigger() -> None:
    text = (_TEMPLATES_DIR / "jobs.html").read_text(encoding="utf-8")
    assert "every 2s" not in text
    assert 'data-ui-sse-channel="jobs"' in text
    assert "data-ui-sse-active-only" in text
    assert "job_detail_live" in text or "job-detail-live" in text
    assert "job_type|urlencode" in text.replace(" ", "") or "job_type|urlencode" in text
    assert "syncJobsLiveUrlsFromLocation" in text


def test_job_detail_live_marks_status_for_active_only() -> None:
    templates = _templates()
    request = MagicMock()
    job = SimpleNamespace(
        id=7,
        type="ongoing",
        status="success",
        started_at=None,
        finished_at=None,
        error=None,
    )
    html = templates.TemplateResponse(
        request,
        "partials/job_detail_live.html",
        {
            "request": request,
            "selected_job": job,
            "logs": [],
        },
    ).body.decode("utf-8")
    assert 'data-selected-job-status="success"' in html
    assert 'data-selected-job-id="7"' in html
    assert 'font-size:0.85rem;">live</span>' not in html


def test_jobs_list_select_uses_htmx() -> None:
    templates = _templates()
    request = MagicMock()
    job = SimpleNamespace(id=12, type="ongoing", status="running")
    html = templates.TemplateResponse(
        request,
        "partials/jobs_list_live.html",
        {
            "request": request,
            "jobs": [job],
            "selected_job": job,
            "job_type": "",
            "status": "",
        },
    ).body.decode("utf-8")
    assert 'hx-get="/jobs/select/live?' in html
    assert 'hx-target="#job-detail-live"' in html
    assert "hx-push-url=" in html


def test_jobs_select_live_partial_has_oob_list() -> None:
    templates = _templates()
    request = MagicMock()
    job = SimpleNamespace(
        id=3,
        type="ongoing",
        status="success",
        started_at=None,
        finished_at=None,
        error=None,
    )
    html = templates.TemplateResponse(
        request,
        "partials/jobs_select_live.html",
        {
            "request": request,
            "jobs": [job],
            "selected_job": job,
            "logs": [],
            "job_type": "",
            "status": "",
        },
    ).body.decode("utf-8")
    assert 'data-selected-job-id="3"' in html
    assert 'id="jobs-list-live" hx-swap-oob="innerHTML"' in html


def test_pipeline_page_no_hx_every_trigger() -> None:
    text = (_TEMPLATES_DIR / "pipeline.html").read_text(encoding="utf-8")
    assert "every 3s" not in text
    assert 'data-ui-sse-channel="pipeline"' in text


def test_pipeline_live_partial_compact_graph() -> None:
    templates = _templates()
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=5,
        info_hash="cd" * 20,
        status="slave_added",
        tg_status="skipped",
        master_added_at=now,
        slave_added_at=now,
        error=None,
        created_at=now,
    )
    request = MagicMock()
    html = templates.TemplateResponse(
        request,
        "partials/pipeline_live.html",
        {
            "request": request,
            "display_rows": [
                {
                    "row": row,
                    "release_name": "Show",
                    "torrent_label": "WEB",
                    "ids_title": "ids",
                    "files_status": "running",
                    "tracked": True,
                }
            ],
        },
    ).body.decode("utf-8")
    assert "gl-pipeline-board" in html
    assert "pipeline-list-table" in html
    assert "pipeline-col-graph" in html
    assert "Δtg" in html
    assert "gl-pipeline-fork" in html
    assert "gl-stage-side" in html
    assert "На slave" not in html
    assert "На master" not in html
    assert ">TG<" not in html
    assert "tg" in html
    assert "check" in html
    assert "cd" * 20 in html
    assert "…" not in html.split("pipeline-list-hash")[1].split("</div>")[0]


def test_archive_files_partial_renders_list(monkeypatch) -> None:
    from app.main import archive_files_partial
    from app.services.releases_view import ArchivePageRow, ReleaseFileRow

    archive = SimpleNamespace(
        id=7, release_id=1, api_present=True, superseded=False, info_hash="ab" * 20
    )
    sibling = SimpleNamespace(
        id=8, release_id=1, api_present=True, superseded=False, info_hash="cd" * 20
    )
    db = MagicMock()
    db.get.return_value = archive
    db.scalars.return_value.all.return_value = [archive, sibling]
    row = ArchivePageRow(
        id=7,
        anime_name="Show",
        release_alias="show",
        category=None,
        torrent_type="WEB",
        torrent_description="1",
        release_id=1,
        torrent_id=10,
        info_hash="ab" * 20,
        file_size=1,
        file_size_label="1 B",
        created_at=None,
        api_present=True,
        superseded=False,
        files=[
            ReleaseFileRow(
                relative_path="ep01.mkv",
                size=1,
                selected=True,
                full_path="/media/ep01.mkv",
                status="ok",
                file_id=5,
                downloadable=True,
            )
        ],
    )
    seen_archives: list = []

    def _build(_db, archives):  # noqa: ANN001
        seen_archives.extend(archives)
        return [row]

    monkeypatch.setattr("app.main.build_archive_page_rows", _build)
    captured: dict = {}

    def _tpl(request, name, ctx):  # noqa: ANN001
        captured["name"] = name
        captured["ctx"] = ctx
        return MagicMock(body=b"ok")

    monkeypatch.setattr("app.main.templates.TemplateResponse", _tpl)
    archive_files_partial(MagicMock(), archive_id=7, allow_downloads="1", db=db)
    assert captured["name"] == "partials/torrent_file_list_items.html"
    assert captured["ctx"]["allow_file_downloads"] is True
    assert captured["ctx"]["files"][0].relative_path == "ep01.mkv"
    assert [a.id for a in seen_archives] == [7, 8]


def test_archive_files_partial_passes_release_siblings_for_removed_filter(
    monkeypatch,
) -> None:
    """Lazy /archive/{id}/files должен видеть siblings — иначе ложный «удалён»."""
    from app.main import archive_files_partial
    from app.services.releases_view import ArchivePageRow, ReleaseFileRow

    target = SimpleNamespace(
        id=10, release_id=99, api_present=True, superseded=False, info_hash="aa" * 20
    )
    sibling = SimpleNamespace(
        id=11, release_id=99, api_present=True, superseded=False, info_hash="bb" * 20
    )
    db = MagicMock()
    db.get.return_value = target
    db.scalars.return_value.all.return_value = [target, sibling]

    captured_archives: list = []

    def _build(_db, archives):  # noqa: ANN001
        captured_archives.extend(list(archives))
        return [
            ArchivePageRow(
                id=10,
                anime_name="Show",
                release_alias="show",
                category=None,
                torrent_type="WEB",
                torrent_description="1",
                release_id=99,
                torrent_id=1,
                info_hash="aa" * 20,
                file_size=1,
                file_size_label="1 B",
                created_at=None,
                api_present=True,
                superseded=False,
                files=[
                    ReleaseFileRow(
                        relative_path="ep01.mkv",
                        size=1,
                        selected=True,
                        full_path=None,
                        status="ok",
                        file_id=1,
                        downloadable=False,
                    )
                ],
            )
        ]

    monkeypatch.setattr("app.main.build_archive_page_rows", _build)
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda *a, **k: MagicMock(body=b"ok"),
    )
    archive_files_partial(MagicMock(), archive_id=10, allow_downloads="0", db=db)
    assert {int(a.id) for a in captured_archives} == {10, 11}


def test_build_archive_page_rows_filters_sibling_orphans(monkeypatch, tmp_path) -> None:
    """С siblings в списке orphan чужого торрента не попадает как «удалён»."""
    from app.services import releases_view as rv
    from app.services.file_tracker import KIND_ORPHAN, KIND_REMOVED

    media = tmp_path / "media"
    show = media / "Show"
    show.mkdir(parents=True)
    own = show / "ep01.mkv"
    sib = show / "ep02.mkv"
    own.write_bytes(b"a")
    sib.write_bytes(b"b")

    monkeypatch.setattr(rv, "resolve_media_root", lambda: media)
    monkeypatch.setattr(rv, "resolve_orphan_scan_root", lambda **_k: show.resolve())

    archive_a = SimpleNamespace(
        id=1,
        anime_name="Show",
        release_alias="show",
        category=None,
        torrent_type="WEB",
        torrent_description="1",
        release_id=5,
        torrent_id=100,
        info_hash="aa" * 20,
        file_size=1,
        created_at=None,
        api_present=True,
        superseded=False,
    )
    archive_b = SimpleNamespace(
        id=2,
        anime_name="Show",
        release_alias="show",
        category=None,
        torrent_type="HEVC",
        torrent_description="1",
        release_id=5,
        torrent_id=101,
        info_hash="bb" * 20,
        file_size=1,
        created_at=None,
        api_present=True,
        superseded=False,
    )
    tf_a = SimpleNamespace(
        relative_path="Show/ep01.mkv",
        size=1,
        selected=True,
        full_path=str(own.resolve()),
        info_hash="aa" * 20,
        ui_status="ok",
        file_index=0,
        id=1,
    )
    tf_b = SimpleNamespace(
        relative_path="Show/ep02.mkv",
        size=1,
        selected=True,
        full_path=str(sib.resolve()),
        info_hash="bb" * 20,
        ui_status="ok",
        file_index=0,
        id=2,
    )

    def _files_by_hash(_db, hashes):  # noqa: ANN001
        mapping = {
            "aa" * 20: [tf_a],
            "bb" * 20: [tf_b],
        }
        return {h: mapping[h] for h in hashes if h in mapping}

    def _events(_db, release_ids):  # noqa: ANN001
        return {
            "aa" * 20: rv._TorrentEvents(
                latest_by_path={},
                removed_candidates=[
                    ("Show/ep02.mkv", str(sib.resolve()), KIND_ORPHAN),
                    ("Show/gone.mkv", str(show / "gone.mkv"), KIND_REMOVED),
                ],
            )
        }

    monkeypatch.setattr(rv, "_files_by_hash", _files_by_hash)
    monkeypatch.setattr(rv, "_recent_events_by_info_hash", _events)
    monkeypatch.setattr(rv, "_disk_hashes_by_path", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_info_hashes_with_active_hash_job", lambda *_a, **_k: set())

    rows = rv.build_archive_page_rows(MagicMock(), [archive_a, archive_b])
    row_a = next(r for r in rows if r.id == 1)
    statuses = {f.relative_path: f.status for f in row_a.files}
    assert "Show/ep02.mkv" not in statuses  # sibling orphan скрыт
    assert statuses.get("Show/gone.mkv") == "removed"
    assert statuses.get("Show/ep01.mkv") == "ok"

    # Без sibling в списке — тот же orphan не отфильтруется (регресс для lazy single-archive).
    rows_alone = rv.build_archive_page_rows(MagicMock(), [archive_a])
    alone = next(r for r in rows_alone if r.id == 1)
    alone_paths = {f.relative_path for f in alone.files}
    assert "Show/ep02.mkv" in alone_paths


def test_torrent_file_list_select_actions_in_body_not_summary() -> None:
    """«Выбрать все» / «Снять» живут в body списка файлов, не в summary."""
    from app.services.releases_view import ReleaseFileRow

    templates = _templates()
    request = MagicMock()
    files = [
        ReleaseFileRow(
            relative_path="ep01.mkv",
            size=1,
            selected=True,
            full_path="/media/ep01.mkv",
            status="ok",
            file_id=5,
            downloadable=True,
        )
    ]
    eager = templates.TemplateResponse(
        request,
        "partials/torrent_file_list.html",
        {
            "request": request,
            "files": files,
            "allow_file_downloads": True,
            "info_hash": "ab" * 20,
        },
    ).body.decode("utf-8")
    assert "torrent-encode-select-actions" in eager
    assert "Выбрать все" in eager
    assert eager.index("torrent-files-body") < eager.index("torrent-encode-select-actions")
    assert eager.index("</summary>") < eager.index("torrent-encode-select-actions")

    items = templates.TemplateResponse(
        request,
        "partials/torrent_file_list_items.html",
        {
            "request": request,
            "files": files,
            "allow_file_downloads": True,
            "info_hash": "ab" * 20,
        },
    ).body.decode("utf-8")
    assert "torrent-encode-select-actions" in items
    assert "Выбрать все" in items
    assert items.index("torrent-files-loaded") < items.index("torrent-encode-select-actions")

    lazy = templates.TemplateResponse(
        request,
        "partials/torrent_file_list_lazy.html",
        {
            "request": request,
            "files_summary": "1 файл.",
            "archive_id": 7,
            "allow_file_downloads": True,
            "info_hash": "ab" * 20,
        },
    ).body.decode("utf-8")
    assert "Выбрать все" not in lazy
    assert "torrent-encode-select-actions" not in lazy
