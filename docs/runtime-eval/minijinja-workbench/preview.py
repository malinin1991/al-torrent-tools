#!/usr/bin/env python3
"""Read-only browser preview of the opt-in workbench on the synthetic database.

Only page/live/static GET routes are installed. Encoder actions, scheduler,
Telegram, qBittorrent and the production application's lifespan are absent.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import functools
import os
from pathlib import Path
import re
import sys
import sysconfig
import threading


class RuntimeAudit:
    """Functional GIL/thread evidence; contains no performance measurements."""

    def __init__(self, expected_gil: bool):
        self.expected_gil = expected_gil
        self.lock = threading.Lock()
        self.main_thread_id = threading.get_ident()
        self.checkpoints = []
        self.worker_thread_ids = set()
        self.calls = 0
        self.completed = 0
        self.failed = 0
        self.active = 0
        self.peak_active = 0
        self.check("startup")

    def check(self, point):
        state = bool(sys._is_gil_enabled())
        record = {"point": point, "gil": state, "thread_id": threading.get_ident()}
        with self.lock:
            self.checkpoints.append(record)
        if state != self.expected_gil:
            raise RuntimeError(f"GIL mismatch at {point}: expected {self.expected_gil}, got {state}")

    @contextmanager
    def rendering(self, name):
        self.check("worker_before_template:" + name)
        with self.lock:
            self.worker_thread_ids.add(threading.get_ident())
            self.calls += 1
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        success = False
        try:
            yield
            self.check("worker_after_template:" + name)
            success = True
        finally:
            with self.lock:
                self.active -= 1
                if success:
                    self.completed += 1
                else:
                    self.failed += 1

    def snapshot(self):
        with self.lock:
            return {
                "python": sys.version, "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                "gil_enabled": bool(sys._is_gil_enabled()), "expected_gil": self.expected_gil,
                "xoptions": dict(sys._xoptions), "forced_gil_off": False,
                "main_thread_id": self.main_thread_id,
                "template_worker_thread_ids": sorted(self.worker_thread_ids),
                "template_calls": self.calls, "completed_calls": self.completed,
                "failed_calls": self.failed, "active_calls": self.active,
                "peak_active_template_responses": self.peak_active,
                "gil_checkpoints": list(self.checkpoints),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=Path("/app"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--media-root", type=Path, default=Path("/bench/media"))
    parser.add_argument("--engine", choices=("current_jinja", "minijinja"), default="minijinja")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--expect-gil", type=int, choices=(0, 1), default=None)
    args = parser.parse_args()
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise SystemExit("Forced GIL-off is forbidden in this preview")
    expected_gil = (not bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
                    if args.expect_gil is None else bool(args.expect_gil))
    audit = RuntimeAudit(expected_gil)
    sys.path.insert(0, str(args.backend.resolve()))
    os.chdir(args.media_root.resolve())
    os.environ.update(DATABASE_URL=args.database_url, ANILIBRIA_MEDIA_ROOT=str(args.media_root.resolve()), APP_ENV="test")
    from sqlalchemy import event
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if url.get_backend_name() != "postgresql" or not re.fullmatch(r"altt_minijinja[a-zA-Z0-9_]*", url.database or ""):
        raise SystemExit("Preview requires the experiment-owned PostgreSQL database")
    from app.utils import datetime_fmt
    from datetime import datetime
    datetime_fmt.utcnow = lambda: datetime(2026, 10, 17)
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from app.api.hevc_workbench import create_hevc_workbench_router
    from app.db.session import engine, SessionLocal
    from app.services.video_kensetsu import video_kensetsu_ui_context
    from experiments.minijinja_workbench.prototype import MiniTemplatesAdapter, TEMPLATES_DIR
    import uvicorn
    audit.check("after_application_imports")

    @event.listens_for(engine, "connect")
    def readonly(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET default_transaction_read_only = on")
        connection.commit()

    @event.listens_for(engine, "before_cursor_execute")
    def readonly_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if not statement.lstrip().upper().startswith(("SELECT", "SHOW", "EXPLAIN")):
            raise RuntimeError("Preview attempted non-read SQL")

    def shell(_request):
        with SessionLocal() as db:
            return video_kensetsu_ui_context(db, refresh=False)

    adapter = (MiniTemplatesAdapter("minijinja", context_processors=[shell]) if args.engine == "minijinja"
               else Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[shell]))
    audit.check("after_environment_construction")
    original_response = adapter.TemplateResponse

    @functools.wraps(original_response)
    def checked_response(*values, **kwargs):
        name = kwargs.get("name") or values[1]
        with audit.rendering(name):
            return original_response(*values, **kwargs)
    adapter.TemplateResponse = checked_response
    app = FastAPI(title="ALTT workbench renderer experiment")
    app.include_router(create_hevc_workbench_router(adapter))
    app.mount("/static", StaticFiles(directory=str(args.backend.resolve() / "app/static")), name="static")

    @app.get("/_experiment/runtime")
    def runtime():
        audit.check("runtime_endpoint")
        return {"engine": args.engine, "database_name": url.database, **audit.snapshot()}

    print(f"Read-only synthetic workbench: {args.engine}; GIL={getattr(sys, '_is_gil_enabled', lambda: True)()}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, loop="asyncio", http="h11", access_log=False)


if __name__ == "__main__":
    main()
