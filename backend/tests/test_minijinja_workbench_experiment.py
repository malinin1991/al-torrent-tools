"""Opt-in renderer parity, snapshot, and unchanged-router regression checks.

These tests require the experiment's pinned MiniJinja in an isolated env. The
ordinary production suite skips this optional module when it is not installed;
the dedicated experiment must report passed tests with zero skips. No external
services, Docker, real qB, or production database are used here.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from importlib.util import find_spec
import re

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from markupsafe import Markup
import pytest

if find_spec("minijinja") is None:
    pytest.importorskip(
        "minijinja", reason="Optional experiment requires minijinja==3.0.1 in its isolated environment",
        exc_type=ModuleNotFoundError,
    )

from app.api import hevc_workbench as routes
from app.db.models import TorrentPipeline
from app.db.session import get_db
from experiments.minijinja_workbench.prototype import (
    LIVE_TEMPLATE, PAGE_TEMPLATE, TEMPLATES_DIR, MiniTemplatesAdapter,
    Renderer, create_app, materialize_context,
)


ENGINES = ("current_jinja", "snapshot_jinja", "minijinja")


def _context(**updates):
    row = {
        "identity": "abc:Show/01.mkv", "file_id": 7, "archive_id": 8,
        "release_id": 42, "torrent_id": 142, "info_hash": "a" * 40,
        "title": "Пример релиза", "relative_path": "Show/01.mkv",
        "alias": "show-alias", "archive_title": "Пример релиза",
        "source": "site", "release_url": "https://example.invalid/releases/show-alias",
        "admin_url": "https://admin.invalid/releases/42", "size_label": "4 GiB",
        "torrent_type": "BDRip 1080p AVC", "episodes": "1-2", "sticky_status": "new",
        "full_path": "/media/Show/01.mkv", "snapshot_at": "2026-10-17T00:00:00Z",
        "anchor_at": "2026-10-15T00:00:00Z", "pair_archive_id": None,
        "pipeline": ("done", None, 3), "hevc_status": "missing",
        "hevc_label": "Нет HEVC", "age_hours": 24.5, "age_from_api": True,
        "overdue_hours": None, "reasons": [], "fs_available": True,
        "availability_changed": False, "receipt": None, "selectable": True,
        "sort_anchor": datetime(2026, 10, 15),
    }
    result = {
        "rows": [row], "changed_rows": [], "view": "all",
        "views": {"all": "Все", "ready": "Готовые", "unknown": "Неоднозначные"},
        "view_urls": {key: f"/hevc-workbench?view={key}" for key in ("all", "ready", "unknown")},
        "counts": {"all": 1, "ready": 1, "unknown": 0}, "checked_count": 1,
        "available_count": 1, "checked_at": "2026-10-17T00:00:00Z",
        "total": 1, "page": 2, "total_pages": 3, "search": "",
        "live_url": "/hevc-workbench/live?view=all&page=2",
        "prev_url": "/hevc-workbench?page=1", "next_url": "/hevc-workbench?page=3",
        "video_kensetsu_enabled": True, "video_kensetsu_ok": True,
        "video_kensetsu_base_url": "https://encoder.invalid",
    }
    result.update(updates)
    return result


class _SemanticHtml(HTMLParser):
    """Decode entities and ignore formatting-only whitespace, retaining DOM."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag, attrs):
        # HTML void elements are equivalent with or without a trailing slash.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.events.append(("text", text))


@pytest.fixture(scope="module")
def renderers():
    # Missing MiniJinja raises with the explicit isolated-env requirement.
    return {engine: Renderer(engine) for engine in ENGINES}


@pytest.mark.parametrize("template_name", (PAGE_TEMPLATE, LIVE_TEMPLATE))
@pytest.mark.parametrize("scenario", ("normal", "empty", "changed", "offsite", "disabled"))
def test_original_templates_have_semantic_parity(renderers, template_name, scenario):
    context = _context()
    row = context["rows"][0]
    if scenario == "empty":
        context.update(rows=[], total=0, checked_count=0, available_count=0)
    elif scenario == "changed":
        row.update(selectable=False, fs_available=False, availability_changed=True,
                   reasons=["Файл пропал после снимка БД"], hevc_status="overdue",
                   hevc_label="Просрочено", overdue_hours=7.5, age_from_api=False,
                   receipt={"history_url": "/encoder-history?submission_id=4", "label": "Неизвестно",
                            "error": "Ответ не получен", "expired": True, "status": "unknown"})
        context.update(view="ready", changed_rows=[row], rows=[])
    elif scenario == "offsite":
        row.update(source="backoffice", release_url=None, file_id=None, full_path="",
                   selectable=False, age_hours=None, pipeline=(None, None, None))
    elif scenario == "disabled":
        context.update(video_kensetsu_enabled=False, video_kensetsu_ok=False)
    html = {engine: renderer.render(template_name, context) for engine, renderer in renderers.items()}
    expected = _SemanticHtml(html["current_jinja"]).events
    assert _SemanticHtml(html["snapshot_jinja"]).events == expected
    assert _SemanticHtml(html["minijinja"]).events == expected
    if scenario == "offsite":
        assert "Нет на сайте" in html["minijinja"]
        assert "На сайте</a>" not in html["minijinja"]
    if scenario == "changed":
        assert "Доступность изменилась при проверке" in html["minijinja"]
        assert "Проверьте журнал перед повтором" in html["minijinja"]


@pytest.mark.parametrize("engine", ENGINES)
def test_hostile_text_attributes_and_urls_are_escaped(renderers, engine):
    hostile = '\"><script id="injected">alert(1)</script>&\' Кириллица /'
    context = _context(search=hostile)
    row = context["rows"][0]
    row.update(title=hostile, relative_path=hostile, identity=hostile,
               archive_title=hostile, alias=hostile, reasons=[hostile],
               receipt={"history_url": '/history?x=" onclick="alert(2)', "label": hostile,
                        "error": hostile, "expired": False, "status": "rejected"})
    html = renderers[engine].render(PAGE_TEMPLATE, context)
    assert '<script id="injected">' not in html
    events = _SemanticHtml(html).events
    assert ("text", hostile) in events
    for event in events:
        if event[0] == "start":
            assert not any(name == "onclick" or name == "id" and value == "injected"
                           for name, value in event[2])
    baseline = renderers["current_jinja"].render(PAGE_TEMPLATE, context)
    assert events == _SemanticHtml(baseline).events


def test_materialization_detaches_mutable_values_and_normalizes_datetime():
    context = _context()
    snapshot = materialize_context(context)
    context["rows"][0]["reasons"].append("later change")
    context["rows"][0]["title"] = "changed title"
    assert snapshot["rows"][0]["reasons"] == []
    assert snapshot["rows"][0]["title"] == "Пример релиза"
    assert snapshot["rows"][0]["sort_anchor"] == "2026-10-15T00:00:00Z"
    assert snapshot["rows"][0]["pipeline"] == ["done", None, 3]
    assert snapshot["request"]["url"]["path"] == "/hevc-workbench"
    assert materialize_context({"html": Markup("<b>unsafe</b>")})["html"].__class__ is str


def test_materialization_rejects_orm_entities_without_attribute_access():
    with pytest.raises(TypeError, match=r"TorrentPipeline at context\.rows\[0\]"):
        materialize_context({"rows": [TorrentPipeline()]})


def test_unchanged_router_runs_same_processors_once_and_keeps_request_query(monkeypatch):
    context = _context()
    monkeypatch.setattr(routes, "list_hevc_workbench", lambda db, **params: context)
    processed = []

    def shell(request):
        processed.append((request.url.path, request.url.query))
        return {"video_kensetsu_enabled": False, "video_kensetsu_ok": False}

    responses = {}
    for engine in ENGINES:
        application = FastAPI()
        templates = (Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[shell])
                     if engine == "current_jinja" else MiniTemplatesAdapter(engine, context_processors=[shell]))
        application.include_router(routes.create_hevc_workbench_router(templates))
        application.dependency_overrides[get_db] = lambda: object()
        with TestClient(application) as client:
            responses[engine] = client.get("/hevc-workbench?view=ready&search=abc")
    assert processed == [("/hevc-workbench", "view=ready&search=abc")] * 3
    assert all(response.status_code == 200 for response in responses.values())
    expected = _SemanticHtml(responses["current_jinja"].text).events
    assert all(_SemanticHtml(response.text).events == expected for response in responses.values())


@pytest.mark.parametrize("engine", ENGINES)
def test_frozen_http_control_is_request_local_under_parallel_requests(engine):
    def provider(request):
        return _context(search=request.query_params["search"])

    app = create_app(engine, provider)
    with TestClient(app) as client:
        terms = [f"isolated-{index}" for index in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda term: client.get("/hevc-workbench", params={"search": term}), terms))
        for term, response in zip(terms, responses):
            assert response.status_code == 200
            assert f'name="search" value="{term}"' in response.text
            assert all(other == term or f'value="{other}"' not in response.text for other in terms)
        live = client.get("/hevc-workbench/live", params={"search": "live"})
        assert live.status_code == 200 and "data-hevc-state" in live.text
        assert "<!doctype html>" not in live.text
