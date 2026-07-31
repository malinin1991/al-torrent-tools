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
        torrents=[],
        archived_torrents=[],
        category=None,
        last_updated=None,
        release_url=None,
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
    assert "Сияй" not in text


def test_pipeline_live_partial_renders_slave_column() -> None:
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
                }
            ],
            "master_states": {},
            "slave_states": {
                "cd" * 20: {
                    "key": "downloading",
                    "label": "загружается",
                    "progress": 0.42,
                    "raw_state": "downloading",
                }
            },
        },
    ).body.decode("utf-8")
    assert "На slave" in html
    assert "gl-pipeline" in html
    assert "загружается" in html
    assert "42%" in html


def test_releases_live_url_uses_urlencode() -> None:
    text = (_TEMPLATES_DIR / "releases.html").read_text(encoding="utf-8")
    assert "search|urlencode" in text
    assert "_uiSsePreserveByTarget" in (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "visibilitychange" in (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
