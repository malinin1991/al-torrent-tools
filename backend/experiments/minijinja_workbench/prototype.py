"""Isolated MiniJinja experiment using the three unchanged HEVC UI templates.

The candidate's measured request boundary is context processors -> detached
snapshot -> native render -> HTMLResponse. ``current_jinja`` retains the real
Request and Python values; ``snapshot_jinja`` is a control that performs the
same materialization as MiniJinja before using the current Jinja renderer.

Production's context service still owns SQL, disk probes, and readiness rules.
This module performs no SQL, filesystem probes, or outbound requests while
rendering. It reads template sources only when constructing a Renderer. All
three templates are preloaded: MiniJinja has no Python loader callback, filter,
global function, or autoescape callback installed by this experiment. Its
built-in HTML autoescape and pycompat behavior are retained.

Renderer construction and warm_cache belong outside warm benchmark timers.
Use render_prepared only for the explicitly render-only comparison; normal
render and TemplateResponse include materialization for both snapshot engines.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from functools import lru_cache
import math
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import DictLoader, Environment as JinjaEnvironment
from starlette.background import BackgroundTask
from starlette.requests import Request

from app.utils.datetime_fmt import as_utc_iso


Engine = Literal["current_jinja", "snapshot_jinja", "minijinja"]
PAGE_TEMPLATE = "hevc_workbench.html"
LIVE_TEMPLATE = "partials/hevc_workbench_live.html"
TEMPLATE_NAMES = ("base.html", PAGE_TEMPLATE, LIVE_TEMPLATE)
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"
SHELL_DEFAULTS = {
    "video_kensetsu_enabled": False,
    "video_kensetsu_ok": False,
    "video_kensetsu_base_url": "",
}
ContextProcessor = Callable[[Request], Mapping[str, Any]]
ContextProvider = Callable[[Request], Mapping[str, Any]]


def _detach(value: Any, location: str) -> Any:
    """Copy only supported values; never retain a Python application object.

    Plain strings discard Markup's trusted-HTML marker deliberately. Datetimes
    follow the project's naive-UTC convention. Mapping/sequence subclasses and
    arbitrary objects (including ORM entities and lazy proxies) are rejected,
    rather than passed to either snapshot renderer for attribute access.
    """
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str):
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError(f"Non-finite float at {location}")
        return value
    if isinstance(value, datetime):
        return as_utc_iso(value)
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"Non-string mapping key at {location}")
            result[key] = _detach(item, f"{location}.{key}")
        return result
    if type(value) in (list, tuple):
        return [_detach(item, f"{location}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"Unsupported render value {type(value).__name__} at {location}")


def _request(path: str, query: str = "") -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "scheme": "http",
        "method": "GET", "path": path, "raw_path": path.encode("utf-8"),
        "root_path": "", "query_string": query.encode("utf-8"), "headers": [],
        "server": ("experiment.invalid", 80), "client": ("127.0.0.1", 1),
    })


def materialize_context(
    context: Mapping[str, Any], *, path: str = "/hevc-workbench",
    shell: Mapping[str, Any] | None = None, request: Request | None = None,
) -> dict[str, Any]:
    """Detach service output plus explicit shell state into plain values.

    A Request is handled here, outside the renderer, and only its URL/navigation
    values are exposed. No request/Session/ORM object reaches the native engine.
    The current templates use only request.url.path; query fields are included
    to make the experimental route boundary explicit and inspectable.
    """
    values = {**SHELL_DEFAULTS, **context, **(shell or {})}
    values.pop("request", None)
    if request is not None:
        path = request.url.path
        query = request.url.query
        query_params = dict(request.query_params)
    else:
        query = ""
        query_params = {}
    values["request"] = {
        "url": {"path": path, "query": query}, "query_params": query_params,
    }
    return _detach(values, "context")


class Renderer:
    """Preloaded renderer shared across requests, with request-local context."""

    def __init__(self, engine: Engine = "minijinja") -> None:
        if engine not in ("current_jinja", "snapshot_jinja", "minijinja"):
            raise ValueError(f"Unknown experimental renderer: {engine}")
        self.engine = engine
        self.sources = {
            name: (TEMPLATES_DIR / name).read_text(encoding="utf-8")
            for name in TEMPLATE_NAMES
        }
        if engine == "minijinja":
            try:
                from minijinja import Environment
            except ImportError as exc:
                raise RuntimeError(
                    "The opt-in experiment requires minijinja==3.0.1 in its "
                    "isolated environment; production dependencies are unchanged."
                ) from exc
            self.env = Environment()
            for name, source in self.sources.items():
                self.env.add_template(name, source)
        elif engine == "snapshot_jinja":
            # Engine-only control: same preloaded sources, no mtime/file checks.
            self.env = JinjaEnvironment(
                loader=DictLoader(self.sources), autoescape=True, auto_reload=False,
            )
            for name in TEMPLATE_NAMES:
                self.env.get_template(name)
        else:
            # Same default environment/options as production Jinja2Templates.
            self.env = Jinja2Templates(directory=str(TEMPLATES_DIR)).env
            for name in TEMPLATE_NAMES:
                self.env.get_template(name)

    def render_prepared(self, template_name: str, context: dict[str, Any]) -> str:
        """Render an already prepared context; excludes snapshot creation."""
        if template_name not in TEMPLATE_NAMES:
            raise ValueError(f"Template outside experiment: {template_name}")
        if self.engine == "minijinja":
            return self.env.render_template(template_name, **context)
        return self.env.get_template(template_name).render(context)

    def render(
        self, template_name: str, context: Mapping[str, Any], *,
        path: str = "/hevc-workbench", shell: Mapping[str, Any] | None = None,
        request: Request | None = None,
    ) -> str:
        if self.engine == "current_jinja":
            prepared = {**SHELL_DEFAULTS, **context, **(shell or {})}
            prepared["request"] = request if request is not None else _request(path)
        else:
            prepared = materialize_context(context, path=path, shell=shell, request=request)
        return self.render_prepared(template_name, prepared)

    def warm_cache(self, context: Mapping[str, Any], *, shell=None) -> None:
        """Warm actual inheritance/includes/macro execution outside a timer."""
        self.render(PAGE_TEMPLATE, context, shell=shell)
        self.render(LIVE_TEMPLATE, context, shell=shell, path="/hevc-workbench/live")


@lru_cache(maxsize=3)
def _renderer(engine: Engine) -> Renderer:
    return Renderer(engine)


def render(engine: Engine, template_name: str, context: Mapping[str, Any], **kwargs) -> str:
    """Convenience entry point; prefer a preconstructed Renderer in benchmarks."""
    return _renderer(engine).render(template_name, context, **kwargs)


def render_current_jinja(context: Mapping[str, Any], *, template_name=PAGE_TEMPLATE, **kwargs) -> str:
    """Render unchanged sources with the current Python/Jinja request context."""
    return render("current_jinja", template_name, context, **kwargs)


class MiniTemplatesAdapter:
    """TemplateResponse interface for the unchanged production router factory.

    Supply the same context_processors to this adapter and the current
    Jinja2Templates baseline, including the production cached Kensetsu lookup.
    They execute exactly once per response, before snapshot materialization.
    The adapter also supports snapshot_jinja as a serialization-cost control.
    """

    def __init__(
        self, engine: Engine = "minijinja", *,
        context_processors: list[ContextProcessor] | None = None,
    ) -> None:
        self.renderer = Renderer(engine)
        self.context_processors = tuple(context_processors or ())

    @property
    def env(self):
        return self.renderer.env

    def TemplateResponse(
        self, request: Request, name: str, context: dict[str, Any] | None = None,
        status_code: int = 200, headers: Mapping[str, str] | None = None,
        media_type: str | None = None, background: BackgroundTask | None = None,
    ) -> HTMLResponse:
        prepared = dict(context or {})
        prepared.setdefault("request", request)
        for processor in self.context_processors:
            prepared.update(processor(request))
        html = self.renderer.render(name, prepared, request=request)
        response = HTMLResponse(
            html, status_code=status_code, headers=headers,
            media_type=media_type, background=background,
        )
        # Match useful test/debug attributes of Starlette's template response.
        response.context = prepared
        response.template = name
        return response


def create_app(
    engine: Engine, context_provider: ContextProvider, *,
    context_processors: list[ContextProcessor] | None = None,
) -> FastAPI:
    """Frozen/provider HTTP control; actual-DB benchmarks should use the router.

    For the actual service boundary, include the unchanged
    app.api.hevc_workbench.create_hevc_workbench_router(MiniTemplatesAdapter(...))
    and override get_db in the harness. This helper intentionally has no DB.
    """
    app = FastAPI()
    adapter = MiniTemplatesAdapter(engine, context_processors=context_processors)
    app.state.experimental_templates = adapter

    @app.get("/hevc-workbench", response_class=HTMLResponse)
    def page(request: Request):
        return adapter.TemplateResponse(request, PAGE_TEMPLATE, dict(context_provider(request)))

    @app.get("/hevc-workbench/live", response_class=HTMLResponse)
    def live(request: Request):
        return adapter.TemplateResponse(request, LIVE_TEMPLATE, dict(context_provider(request)))

    return app
