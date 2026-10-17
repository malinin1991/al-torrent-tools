#!/usr/bin/env python3
"""Opt-in, isolated HEVC workbench renderer experiment.

One invocation records one process/sample. The controller must interleave
engines/runtimes in eight paired rounds; this runner never retries a sample to
obtain a preferred result. Primary HTTP contract: eight requests, concurrency
one or four, regular 3.15 or default-GIL-off 3.15t. No production lifespan,
scheduler, outbound service call, or forced GIL-off option is used.

Run from backend, with the experiment's optional MiniJinja installed. Prepare
only an experiment-owned PostgreSQL database whose name starts altt_minijinja.
Measurement connections are read-only. Renderer construction, connection and
worker warmup, assertions, HTML parsing and hashes are outside batch timers.
Normal render timers include candidate snapshot materialization.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import functools
import gc
import hashlib
from html.parser import HTMLParser
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import resource
import statistics
import sys
import sysconfig
import threading
import time
import traceback
from urllib.parse import urlencode, urlsplit


NOW = datetime(2026, 10, 17)
OLD_NOW = datetime(2026, 10, 13)
PROTOCOL = {
    "version": 1, "rounds": 8, "http_requests": 8, "render_calls": 64,
    "primary_modes": ["regular", "ft_off"],
    "primary_engines": ["current_jinja", "minijinja"], "concurrency": [1, 4],
    "p95": "exploratory; resample paired rounds/batches, not individual concurrent requests",
    "transport": "one ASGI event loop and shared Environment per process; no TCP/TLS/browser",
    "clock": NOW.isoformat(), "warmup_concurrency": 4,
}
SAFE_DB = re.compile(r"altt_minijinja[a-zA-Z0-9_]*\Z")
LIVE_URL = "/hevc-workbench/live?view=all&page=1&per_page=30"
TEMPLATE_MAP = {"live": "partials/hevc_workbench_live.html", "page": "hevc_workbench.html"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def gil():
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


class Audit:
    def __init__(self, mode, allow_other_runtime=False):
        self.mode = mode
        self.allow_other_runtime = allow_other_runtime
        self.lock = threading.Lock()
        self.checkpoints = []
        self.events = []
        self.counts = {}
        self.active = {"route": 0, "render": 0, "context": 0}
        self.peaks = dict(self.active)
        self.phase = "setup"
        self.check("process_start")

    def check(self, point):
        state = gil()
        with self.lock:
            self.checkpoints.append({"point": point, "gil": state,
                                     "thread_id": threading.get_ident(), "phase": self.phase})
        expected = self.mode != "ft_off"
        if state != expected:
            raise RuntimeError(f"GIL mode mismatch at {point}: expected {expected}, got {state}; never force GIL off")
        if not self.allow_other_runtime:
            build = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
            if build != (self.mode != "regular") or sys.version_info[:2] != (3, 15):
                raise RuntimeError("Expected CPython 3.15 regular/free-threaded build; --allow-other-runtime is smoke-only")

    def reset(self, phase):
        with self.lock:
            assert not any(self.active.values()), self.active
            self.phase = phase
            self.events = []
            self.counts = {"sql_reads": 0, "sql_writes": 0, "commits": 0}
            self.peaks = dict(self.active)

    @contextmanager
    def span(self, kind):
        self.check(f"{kind}_enter")
        started = time.perf_counter()
        with self.lock:
            self.active[kind] += 1
            self.peaks[kind] = max(self.peaks[kind], self.active[kind])
        try:
            yield
        finally:
            with self.lock:
                self.active[kind] -= 1
                self.events.append({"kind": kind, "thread_id": threading.get_ident(),
                                    "start": started, "end": time.perf_counter(), "phase": self.phase})
            self.check(f"{kind}_exit")

    def snapshot(self):
        with self.lock:
            return {"counts": self.counts.copy(), "peaks": self.peaks.copy(),
                    "events": list(self.events), "gil_checkpoints": list(self.checkpoints)}


def configure(args):
    sys.path.insert(0, str(args.source_backend.resolve()))
    sys.path.insert(0, str(args.source_backend.resolve() / "scripts"))
    os.environ.update(DATABASE_URL=args.database_url, ANILIBRIA_MEDIA_ROOT=str(args.media_root.resolve()),
                      TORRENT_STORAGE_DIR=str(args.media_root.resolve() / "torrents"), APP_ENV="test",
                      SECRET_KEY="minijinja-isolated-experiment", ANILIBRIA_ADMIN_BASE_URL="",
                      ANILIBRIA_BASE_URL="http://invalid.invalid", ANILIBRIA_FALLBACK_BASE_URL="http://invalid.invalid")
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise RuntimeError("Forced GIL-off is forbidden in this experiment")
    # No .env from the user's checkout. The app otherwise uses normal resolvers.
    os.chdir(args.media_root.resolve())
    from app.utils import datetime_fmt
    datetime_fmt.utcnow = lambda: NOW


def check_database(database_url):
    from sqlalchemy.engine import make_url
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not SAFE_DB.fullmatch(url.database or ""):
        raise ValueError("Use an isolated PostgreSQL database named altt_minijinja...")
    return url


def memory():
    result = {"peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024))}
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        result.update({name: int(fields[key].split()[0]) * 1024 for key, name in
                       (("VmRSS", "rss_bytes"), ("VmHWM", "vm_hwm_bytes"))})
    except (OSError, KeyError, ValueError):
        pass
    return result


def provenance(args, audit):
    import benchmark_runtime as common
    from benchmark_free_threading import cpu_constraints
    import psycopg
    record = common.metadata()
    audit.check("provenance_common_metadata")
    record.update(cpu_constraints=cpu_constraints(), xoptions=dict(sys._xoptions),
                  psycopg_backend=psycopg.pq.__impl__, libpq_version=psycopg.pq.version(),
                  source_backend=str(args.source_backend.resolve()), protocol=PROTOCOL,
                  database_name=check_database(args.database_url).database,
                  source_files={str(path.relative_to(args.source_backend)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in [args.source_backend / "scripts/benchmark_minijinja_workbench.py",
                                             args.source_backend / "experiments/minijinja_workbench/prototype.py",
                                             args.source_backend / "app/api/hevc_workbench.py",
                                             args.source_backend / "app/services/hevc_workbench.py",
                                             *(args.source_backend / "app/templates" / name for name in
                                               ("base.html", "hevc_workbench.html", "partials/hevc_workbench_live.html"))]})
    for name in ("jinja2", "markupsafe", "minijinja", "minijinja._lowlevel"):
        try:
            module = importlib.import_module(name)
            record.setdefault("renderer_modules", {})[name] = getattr(module, "__file__", None)
        except ImportError as exc:
            record.setdefault("renderer_modules", {})[name] = {"unavailable": str(exc)}
        audit.check("provenance_import_" + name)
    return record


def prepare(args):
    """Create/reset only the named experiment DB and owned tiny-file fixture."""
    from sqlalchemy import create_engine, insert, select, text
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.db.models import (EncoderSubmission, EncoderSubmissionItem, Job, Release,
                               Setting, TorrentArchive, TorrentFile, TorrentPipeline)
    import benchmark_runtime as common
    url = check_database(args.database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT",
                          connect_args={"prepare_threshold": None})
    with admin.connect() as connection:
        exists = connection.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": url.database}).scalar()
        if not exists:
            connection.execute(text("CREATE DATABASE " + admin.dialect.identifier_preparer.quote(url.database)))
    admin.dispose()
    root = args.media_root.resolve()
    marker = root / ".altt_minijinja_fixture.json"
    if any(root.iterdir()) and not marker.exists():
        raise RuntimeError("Media fixture directory must be empty or contain its altt_minijinja marker")
    if marker.exists() and json.loads(marker.read_text()).get("database_name") != url.database:
        raise RuntimeError("Media fixture belongs to a different experiment database")
    # Claim the previously empty fixture before writes, so an interrupted
    # preparation can be safely and deterministically repeated.
    marker.write_text(json.dumps({"database_name": url.database, "status": "preparing"}) + "\n")
    engine = create_engine(url, connect_args={"prepare_threshold": None})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE " + ",".join(engine.dialect.identifier_preparer.quote(table.name)
                                for table in Base.metadata.sorted_tables) + " RESTART IDENTITY CASCADE"))
    rows, keys = common.hevc_fixture()
    releases, archives, files = [], [], []
    delta = NOW - OLD_NOW
    for block in range(8):
        shift = block * 128
        for rid in range(1, 129):
            nrid = rid + shift
            releases.append(dict(release_id=nrid, release_alias=f"bench-{nrid}",
                                 title=f"Benchmark release {nrid:05}", source="site", genres_json=["Benchmark"], is_abandoned=False))
        for row in rows:
            values = vars(row).copy()
            aid = row.id + block * 12800
            values.update(id=aid, release_id=row.release_id + shift, torrent_id=aid,
                          info_hash=f"{aid:040x}", file_path=str(root / "torrents" / f"{aid}.torrent"),
                          release_alias=f"bench-{row.release_id + shift}", anime_name=f"Benchmark release {row.release_id + shift:05}",
                          created_at=row.created_at + delta, api_created_at=row.api_created_at + delta)
            archives.append(values)
            for index, key in enumerate(sorted(keys[row.id])):
                path = root / "hevc" / str(aid) / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"isolated complete source fixture\n")
                files.append(dict(torrent_id=aid, info_hash=values["info_hash"], release_id=values["release_id"],
                                  relative_path=key, full_path=str(path), size=path.stat().st_size, file_index=index,
                                  selected=True, ui_status="new", media_present=True, is_checking=False,
                                  created_at=NOW, updated_at=NOW))
    # Keep all branch fixtures in the same static data set used by both engines.
    releases[0].update(title='Опасное <b>&"\' / имя 😀', source="backoffice", release_alias="bench-special")
    with Session(engine, autoflush=False) as db:
        db.execute(insert(Release), releases)
        db.execute(insert(TorrentArchive), archives)
        for start in range(0, len(files), 2000):
            db.execute(insert(TorrentFile), files[start:start + 2000])
        db.add_all(Setting(key=key, value=value, updated_at=NOW) for key, value in {
            "video_kensetsu_enabled": "true", "video_kensetsu_base_url": "http://encoder.invalid",
            "video_kensetsu_health_ok": "true", "video_kensetsu_health_checked_at": NOW.isoformat() + "+00:00",
            "anilibria_admin_url_template": "https://admin.invalid/releases/{release_id}",
        }.items())
        db.flush()
        visible = list(db.scalars(select(TorrentFile).where(TorrentFile.info_hash.in_([f"{rid * 100 + 4:040x}" for rid in (1, 2, 3, 6)]))
                                 .order_by(TorrentFile.id)))
        for index, file in enumerate(visible[:8]):
            state = ("accepted", "unknown", "sending", "rejected")[index % 4]
            group = EncoderSubmission(request_id=f"bench-{index}", request_digest="a" * 64,
                                      base_url="http://encoder.invalid", video_id="hevc", audio_id="opus",
                                      expires_at=NOW - timedelta(hours=1), request_started_at=NOW - timedelta(hours=2),
                                      finished_at=None if state == "sending" else NOW, created_at=NOW, updated_at=NOW,
                                      items=[EncoderSubmissionItem(file_id=file.id, release_id=file.release_id,
                                             info_hash=file.info_hash, relative_path=file.relative_path, full_path=file.full_path,
                                             status=state, error='Тест <error>&"' if state == "rejected" else None)])
            db.add(group)
        if visible:
            file = visible[-1]
            db.add(Job(type="hash_torrent", status="pending", params_json={"info_hash": file.info_hash}, created_at=NOW))
            db.add(TorrentPipeline(info_hash=file.info_hash, release_id=file.release_id, torrent_id=file.torrent_id,
                                   status="done", created_at=NOW, error='fixture <detail>&"'))
        # Distinct FS and DB unavailability without changing sticky history.
        if len(visible) > 8:
            # The ninth release-1 file has no receipt and is DB-ready, so the
            # ready+search correctness case reaches changed_rows after FS probe.
            Path(visible[8].full_path).unlink()
            visible[2].selected = False
        db.commit()
    with engine.begin() as connection:
        connection.execute(text("ANALYZE"))
    manifest = {"protocol": PROTOCOL, "database_name": url.database, "media_root": str(root),
                "releases": len(releases), "archives": len(archives), "files": len(files),
                "input_sha256": digest({"archives": archives, "files": files, "releases": releases})}
    marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    engine.dispose()
    return manifest


class SemanticHTML(HTMLParser):
    """Entity-insensitive DOM events; preserve attributes and code/script text."""
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.events, self.stack = [], []
        self.feed(value)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.events.append(["start", tag, sorted(attrs)])
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.events.append(["end", tag])
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if not any(tag in {"pre", "code", "script", "style", "textarea"} for tag in self.stack):
            data = re.sub(r"\s+", " ", data).strip()
        if data:
            self.events.append(["text", data])


def body_record(body):
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    events = SemanticHTML(body).events
    keys = [dict(item[2]).get("data-source-key") for item in events if item[0] == "start" and "data-source-key" in dict(item[2])]
    return {"html_sha256": hashlib.sha256(body.encode()).hexdigest(), "semantic_sha256": digest(events),
            "body_bytes": len(body.encode()), "row_keys": keys, "row_count": len(keys)}


def setup_application(args, audit, selected=None):
    from sqlalchemy import event
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from app.db.session import engine, SessionLocal
    from app.services.video_kensetsu import video_kensetsu_ui_context
    from app.api import hevc_workbench as routes
    from experiments.minijinja_workbench.prototype import MiniTemplatesAdapter, TEMPLATES_DIR
    audit.check("application_native_imports")

    if not getattr(engine, "_minijinja_audit_installed", False):
        @event.listens_for(engine, "connect")
        def readonly(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute("SET default_transaction_read_only = on")
            connection.commit()

        @event.listens_for(engine, "before_cursor_execute")
        def sql(_conn, _cursor, statement, _parameters, _context, _many):
            is_read = statement.lstrip().upper().startswith(("SELECT", "SHOW", "EXPLAIN"))
            with audit.lock:
                key = "sql_reads" if is_read else "sql_writes"
                audit.counts[key] = audit.counts.get(key, 0) + 1
            if not is_read:
                raise RuntimeError("Measurement attempted non-read SQL: " + statement[:60])

        @event.listens_for(engine, "commit")
        def commit(_conn):
            with audit.lock:
                audit.counts["commits"] = audit.counts.get("commits", 0) + 1

        engine._minijinja_audit_installed = True
        engine.dispose()  # New connections receive read-only mode.
        original_context = routes.list_hevc_workbench

        @functools.wraps(original_context)
        def context(*values, **kwargs):
            with audit.span("context"):
                return original_context(*values, **kwargs)
        routes.list_hevc_workbench = context

    def kensetsu_context(_request):
        # Exact logic of app.main._video_kensetsu_template_context. Avoid
        # importing unrelated app.main handlers/jobs into this isolated app.
        with SessionLocal() as db:
            return video_kensetsu_ui_context(db, refresh=False)
    name = selected or args.engine
    adapter = (Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[kensetsu_context])
               if name == "current_jinja" else MiniTemplatesAdapter(name, context_processors=[kensetsu_context]))
    original_response = adapter.TemplateResponse

    @functools.wraps(original_response)
    def response(*values, **kwargs):
        with audit.span("render"):
            return original_response(*values, **kwargs)
    adapter.TemplateResponse = response
    app = FastAPI()
    app.include_router(routes.create_hevc_workbench_router(adapter))

    class Probe:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            with audit.span("route"):
                await app(scope, receive, send)
    audit.check("environment_constructed")
    return Probe(), adapter, engine


async def http_batch(client, url, concurrency, count):
    semaphore = asyncio.Semaphore(concurrency)
    async def one(index):
        async with semaphore:
            started = time.perf_counter()
            response = await client.get(url)
            elapsed = time.perf_counter() - started
            return {"index": index, "elapsed_sec": elapsed, "status": response.status_code,
                    # Decode/hash/assert after stopping the whole batch timer.
                    "body": response.content}
    return await asyncio.gather(*(one(index) for index in range(count)))


async def measure_http(args, audit):
    import anyio.to_thread
    import httpx
    app, _adapter, _engine = setup_application(args, audit)
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = 4
    audit.reset("warmup")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://experiment.invalid") as client:
        warmed = await http_batch(client, args.url, 4, 4)
        assert all(row["status"] == 200 for row in warmed)
        expected = body_record(warmed[0]["body"])
        assert all(body_record(row["body"])["semantic_sha256"] == expected["semantic_sha256"] for row in warmed)
        warmup = audit.snapshot()
        worker_ids = sorted({row["thread_id"] for row in warmup["events"] if row["kind"] == "context"})
        assert len(worker_ids) == 4, f"Warmup did not exercise four route workers: {worker_ids}"
        audit.check("after_warmup")
        gc.collect()
        before = memory()
        audit.reset("measure")
        cpu, started = time.process_time(), time.perf_counter()
        responses = await http_batch(client, args.url, args.concurrency, args.requests)
        wall = time.perf_counter() - started
        cpu = time.process_time() - cpu
    audit.check("after_measured_batch")
    return measured_record(args, audit, responses, expected, wall, cpu, before, warmup,
                           extra={"anyio_thread_tokens": limiter.total_tokens, "warm_route_worker_ids": worker_ids})


def measured_record(args, audit, responses, expected, wall, cpu, before, warmup, extra=None):
    for row in responses:
        assert row["status"] == 200, row["body"][:300]
        result = body_record(row.pop("body"))
        assert result["semantic_sha256"] == expected["semantic_sha256"], "Response changed within static fixture"
        row.update(result)
    snapshot = audit.snapshot()
    assert snapshot["counts"].get("sql_writes", 0) == snapshot["counts"].get("commits", 0) == 0
    latencies = [row["elapsed_sec"] for row in responses]
    return {"kind": args.child, "engine": args.engine, "mode": args.mode, "concurrency": args.concurrency,
            "requests": len(responses), "url": args.url, "template": args.template,
            "wall_sec": wall, "cpu_sec": cpu, "cpu_average_cores": cpu / wall,
            "throughput_per_sec": len(responses) / wall, "latency_median_sec": statistics.median(latencies),
            "latency_p95_sec": percentile(latencies, .95), "latencies_sec": latencies, "responses": responses,
            "memory_before": before, "memory_after": memory(), "measurement": snapshot,
            "warmup": warmup, "expected_body": expected, **(extra or {})}


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def measure_render(args, audit):
    from app.db.session import SessionLocal
    from app.services.hevc_workbench import list_hevc_workbench
    from app.services.video_kensetsu import video_kensetsu_ui_context
    from experiments.minijinja_workbench.prototype import Renderer, _request, materialize_context
    # Install the same database-read-only guard, but do not include SQL in renders.
    setup_application(args, audit)
    parsed = urlsplit(args.url)
    request = _request(parsed.path, parsed.query)
    with SessionLocal() as db:
        context = list_hevc_workbench(db, now=NOW)
    with SessionLocal() as db:
        shell = video_kensetsu_ui_context(db, refresh=False)
    renderer = Renderer(args.engine)
    template = TEMPLATE_MAP[args.template]
    renderer.warm_cache(context, shell=shell)
    prepared = materialize_context(context, shell=shell, request=request) if args.render_boundary == "prepared" else None
    def render():
        with audit.span("render"):
            if prepared is not None:
                return renderer.render_prepared(template, prepared)
            return renderer.render(template, context, request=request, shell=shell)
    expected = body_record(render())
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="render-bench") as pool:
        barrier = threading.Barrier(4)
        def warm(_index):
            barrier.wait()
            return render()
        audit.reset("warmup")
        list(pool.map(warm, range(4)))
        warmup = audit.snapshot()
        audit.check("after_warmup")
        gc.collect()
        before = memory()
        audit.reset("measure")
        semaphore = threading.Semaphore(args.concurrency)
        def one(index):
            with semaphore:
                started = time.perf_counter()
                body = render()
                return {"index": index, "elapsed_sec": time.perf_counter() - started, "status": 200, "body": body}
        cpu, started = time.process_time(), time.perf_counter()
        responses = list(pool.map(one, range(args.requests)))
        wall = time.perf_counter() - started
        cpu = time.process_time() - cpu
    audit.check("after_measured_batch")
    return measured_record(args, audit, responses, expected, wall, cpu, before, warmup,
                           extra={"render_boundary": args.render_boundary, "context_prepared_outside_timer": True})


async def correctness(args, audit):
    import anyio.to_thread
    import httpx
    anyio.to_thread.current_default_thread_limiter().total_tokens = 4
    urls = [f"{path}?" + urlencode(params) for path in ("/hevc-workbench", "/hevc-workbench/live")
            for params in [*(dict(view=view, page=1, per_page=30) for view in
                             ("all", "ready", "overdue", "unavailable", "unknown")),
                           dict(view="all", page=2, per_page=30), dict(view="all", search="Опасное", per_page=30),
                           dict(view="ready", search="Опасное", per_page=30),
                           dict(view="all", search='<missing>&"\' 😀', per_page=30)]]
    outputs, records = {}, []
    for selected in ("current_jinja", "snapshot_jinja", "minijinja"):
        app, _adapter, _engine = setup_application(args, audit, selected)
        outputs[selected] = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://experiment.invalid") as client:
            for url in urls:
                response = await client.get(url)
                assert response.status_code == 200, response.text[:400]
                outputs[selected][url] = response.text
                record = body_record(response.text)
                entry = {"engine": selected, "url": url, **record}
                if "search=" in url and "missing" not in url:
                    assert record["row_count"] > 0, "Escaping/offsite fixture not reached"
                    assert "Нет на сайте" in response.text, "Offsite fixture was not displayed"
                    assert "&lt;b&gt;" in response.text and "<b>" not in response.text, "Title escaping changed"
                    entry["offsite_and_escaping_fixture_reached"] = True
                if "view=unknown" in url:
                    assert record["row_count"] > 0 and "Приём не подтверждён" in response.text
                    entry["unknown_receipt_fixture_reached"] = True
                if "view=ready" in url and "search=" in url:
                    assert "Доступность изменилась при проверке" in response.text
                    assert any(item[0] == "start" and item[1] == "input" and
                               "disabled" in dict(item[2]) for item in SemanticHTML(response.text).events)
                    entry["changed_rows_fixture_reached"] = True
                if "view=unavailable" in url:
                    assert record["row_count"] > 0
                    entry["unavailable_fixture_reached"] = True
                if selected != "current_jinja":
                    baseline = outputs["current_jinja"][url]
                    assert SemanticHTML(baseline).events == SemanticHTML(response.text).events, f"DOM mismatch: {selected} {url}"
                    entry["byte_equal"] = baseline == response.text
                    if baseline != response.text:
                        difference = next((index for index, pair in enumerate(zip(baseline, response.text)) if pair[0] != pair[1]), min(len(baseline), len(response.text)))
                        entry["first_byte_difference"] = {"index": difference,
                            "baseline": baseline[max(0, difference - 50):difference + 100],
                            "candidate": response.text[max(0, difference - 50):difference + 100]}
                records.append(entry)
    audit.check("after_correctness")
    return {"kind": "correctness", "cases": records, "passed": True,
            "comparison": "DOM tags/ordered text/all attribute values; only HTML entities and formatting whitespace normalized",
            "audit": audit.snapshot()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--correctness", action="store_true")
    parser.add_argument("--child", choices=("http", "render", "correctness", "provenance"))
    parser.add_argument("--engine", choices=("current_jinja", "snapshot_jinja", "minijinja"), default="current_jinja")
    parser.add_argument("--mode", choices=("regular", "ft_on", "ft_off"), default="regular")
    parser.add_argument("--concurrency", type=int, choices=(1, 4), default=1)
    parser.add_argument("--requests", type=int)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--source-backend", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default=LIVE_URL)
    parser.add_argument("--template", choices=("live", "page"), default="live")
    parser.add_argument("--render-boundary", choices=("adapter", "prepared"), default="adapter")
    parser.add_argument("--allow-other-runtime", action="store_true", help="Local smoke only; marks result non-comparable")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.media_root = args.media_root.resolve()
    args.source_backend = args.source_backend.resolve()
    args.media_root.mkdir(parents=True, exist_ok=True)
    if args.correctness:
        args.child = "correctness"
    if not args.prepare and not args.child:
        parser.error("Choose --prepare or --child")
    if args.requests is None:
        args.requests = 64 if args.child == "render" else 8
    if args.requests < 1:
        parser.error("--requests must be positive")
    audit = None
    try:
        audit = Audit(args.mode, args.allow_other_runtime)
        configure(args)
        check_database(args.database_url)
        if args.prepare:
            result = prepare(args)
        elif args.child == "provenance":
            result = provenance(args, audit)
        elif args.child == "http":
            result = asyncio.run(measure_http(args, audit))
        elif args.child == "render":
            result = measure_render(args, audit)
        else:
            result = asyncio.run(correctness(args, audit))
        audit.check("process_end")
        result.update(protocol=PROTOCOL, smoke_only=args.allow_other_runtime, gil=gil(),
                      py_gil_disabled=sysconfig.get_config_var("Py_GIL_DISABLED"), python=sys.version,
                      process_gil_checkpoints=audit.checkpoints)
        exit_code = 0
    except Exception as exc:
        result = {"failed": True, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                  "audit": audit.snapshot() if audit else None, "engine": args.engine, "mode": args.mode, "kind": args.child}
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(args.output), "failed": bool(result.get("failed")),
                      "kind": args.child, "engine": args.engine, "mode": args.mode,
                      "wall_sec": result.get("wall_sec"), "gil": gil()}, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
