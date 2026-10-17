#!/usr/bin/env python3
"""Actual-PostgreSQL differential correctness, without timing or fixture writes.

The captured, SHA-verified pre-optimization service is the oracle. Each service
call gets a fresh read-only Session/transaction, and complete contexts are
compared recursively, retaining types, mapping order, row order and checked_at.
Live URL checks parse the service-generated query parameters and repeat the
comparison; this is a service test, not an HTTP/browser or performance test.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import sysconfig
import traceback
from urllib.parse import parse_qs, urlsplit


NOW = datetime(2026, 10, 17)
BASELINE_SHA256 = "0ed990e00bb6e4e1c2bff88d48b1490a09e926d66f3f147b2929e95a289fb8db"
DATABASE_NAME = re.compile(r"(?:altt_minijinja|altt_workbench)[A-Za-z0-9_]*\Z")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported context value: {type(value).__name__}")


def context_digest(context):
    # Preserve insertion order, as the templates iterate views/view_urls.
    raw = json.dumps(context, ensure_ascii=False, sort_keys=False, allow_nan=False, default=serialize)
    return hashlib.sha256(raw.encode()).hexdigest()


def differences(expected, actual, path="context", limit=10):
    """Exact nested comparison, including container/scalar types and key order."""
    result = []
    def visit(left, right, where):
        if len(result) >= limit:
            return
        if type(left) is not type(right):
            result.append({"path": where, "issue": "type", "baseline": type(left).__name__, "candidate": type(right).__name__})
        elif isinstance(left, dict):
            if list(left) != list(right):
                result.append({"path": where, "issue": "keys_or_mapping_order", "baseline": list(left), "candidate": list(right)})
            for key in left.keys() & right.keys():
                visit(left[key], right[key], f"{where}.{key}")
        elif isinstance(left, (list, tuple)):
            if len(left) != len(right):
                result.append({"path": where, "issue": "length", "baseline": len(left), "candidate": len(right)})
            for index, (a, b) in enumerate(zip(left, right)):
                visit(a, b, f"{where}[{index}]")
        elif left != right:
            result.append({"path": where, "issue": "value", "baseline": repr(left)[:300], "candidate": repr(right)[:300]})
    visit(expected, actual, path)
    return result


def cases():
    result = [(f"view-{view}", {"view": view}) for view in ("all", "ready", "overdue", "unavailable", "unknown")]
    return result + [
        ("page-two", {"page": 2}),
        ("page-extreme-high", {"page": 10**9}),
        ("page-negative", {"page": -100}),
        ("per-page-zero", {"per_page": 0}),
        ("per-page-extreme-high", {"per_page": 10**6}),
        ("view-invalid", {"view": "invalid-view"}),
        ("search-offsite-unicode", {"search": "Опасное"}),
        ("ready-search-fs-changed", {"view": "ready", "search": "Опасное"}),
        ("unknown-search-receipt", {"view": "unknown", "search": "Опасное"}),
        ("search-alias", {"search": "bench-1"}),
        ("search-file-path", {"search": "episode-01.mkv"}),
        ("search-whitespace", {"search": "   Benchmark release 00002   "}),
        ("search-html-empty", {"search": '<missing>&"\' 😀'}),
        ("search-none", {"search": None}),
        ("search-truncation", {"search": "x" * 250}),
    ]


def live_params(context):
    url = urlsplit(context["live_url"])
    if url.path != "/hevc-workbench/live":
        raise AssertionError("Unexpected live URL path")
    query = parse_qs(url.query, keep_blank_values=True)
    if set(query) != {"view", "search", "page", "per_page"} or any(len(value) != 1 for value in query.values()):
        raise AssertionError("Unexpected/missing/duplicate live query parameters")
    return {"view": query["view"][0], "search": query["search"][0],
            "page": int(query["page"][0]), "per_page": int(query["per_page"][0])}


class Observer:
    def __init__(self, args):
        self.args, self.current, self.gil_points, self.calls = args, None, [], 0
        self.report, self.last_call = None, None
        self.check_gil("process_start")

    def check_gil(self, point):
        enabled = bool(getattr(sys, "_is_gil_enabled", lambda: True)())
        self.gil_points.append({"point": point, "gil": enabled})
        if enabled != (self.args.mode != "ft_off"):
            raise RuntimeError(f"GIL mismatch at {point}; never force GIL off")
        if not self.args.allow_other_runtime:
            if sys.version_info[:2] != (3, 15) or bool(sysconfig.get_config_var("Py_GIL_DISABLED")) != (self.args.mode != "regular"):
                raise RuntimeError("Use the selected CPython 3.15 build, or --allow-other-runtime for smoke only")

    def install(self, engine, session_class):
        from sqlalchemy import event
        @event.listens_for(engine, "connect")
        def readonly(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute("SET default_transaction_read_only = on")
            connection.commit()

        @event.listens_for(engine, "before_cursor_execute")
        def statement(_connection, _cursor, sql, parameters, _context, _many):
            is_read = sql.lstrip().upper().startswith(("SELECT", "SHOW", "EXPLAIN"))
            if self.current is not None:
                self.current["reads" if is_read else "writes"] += 1
                is_file_query = bool(re.search(r"\bFROM\s+\"?torrent_files\"?\b", sql, re.IGNORECASE))
                self.current["statement_digests"].append(hashlib.sha256(sql.encode()).hexdigest())
                if is_file_query:
                    hashes = parameters.get("workbench_active_hashes") if isinstance(parameters, Mapping) else None
                    self.current["file_queries"].append({"postgresql_case": bool(re.search(r"\bCASE\b", sql)),
                        "active_hash_parameter_count": len(hashes) if hashes is not None else None,
                        "statement_sha256": hashlib.sha256(sql.encode()).hexdigest()})
            if not is_read:
                raise AssertionError("Service attempted non-read SQL: " + sql[:100])

        @event.listens_for(engine, "commit")
        def commit(_connection):
            if self.current is not None:
                self.current["commits"] += 1
                raise AssertionError("Service attempted a commit")

        @event.listens_for(session_class, "before_flush")
        def flush(_session, _context, _instances):
            if self.current is not None:
                self.current["flushes"] += 1
                raise AssertionError("Service attempted a flush")
        engine.dispose()

    def invoke(self, factory, service, params, label):
        self.calls += 1
        state = {"call": self.calls, "reads": 0, "writes": 0, "flushes": 0, "commits": 0,
                 "file_queries": [], "statement_digests": [], "dirty_before": None, "dirty_after": None}
        self.last_call = state
        self.check_gil(label + "_before")
        self.current = state
        try:
            with factory() as db:
                # Explicit transaction, intentionally rolled back by Session.close.
                # No context-manager commit occurs after a successful read.
                db.begin()
                state["dirty_before"] = [len(db.new), len(db.dirty), len(db.deleted)]
                context = service(db, now=NOW, **params)
                state["dirty_after"] = [len(db.new), len(db.dirty), len(db.deleted)]
                if state["dirty_before"] != [0, 0, 0] or state["dirty_after"] != [0, 0, 0]:
                    raise AssertionError("Service changed ORM unit-of-work state")
        finally:
            self.current = None
            self.check_gil(label + "_after")
        if not state["reads"] or state["writes"] or state["flushes"] or state["commits"]:
            raise AssertionError("Read-only observer failed")
        return context, state


def run(args, observer):
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if url.get_backend_name() != "postgresql" or not DATABASE_NAME.fullmatch(url.database or ""):
        raise ValueError("Use an experiment-owned PostgreSQL database altt_minijinja.../altt_workbench...")
    if not args.media_root.is_dir():
        raise ValueError("Existing isolated media fixture is required")
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise RuntimeError("Forced GIL-off is forbidden")
    sys.path.insert(0, str(args.source_backend))
    os.environ.update(DATABASE_URL=args.database_url, ANILIBRIA_MEDIA_ROOT=str(args.media_root),
                      TORRENT_STORAGE_DIR=str(args.media_root / "torrents"), APP_ENV="test",
                      SECRET_KEY="hevc-differential-isolated", ANILIBRIA_BASE_URL="http://invalid.invalid",
                      ANILIBRIA_FALLBACK_BASE_URL="http://invalid.invalid", ANILIBRIA_ADMIN_BASE_URL="")
    os.chdir(args.media_root)  # Settings never reads checkout .env.
    from app.utils import datetime_fmt
    datetime_fmt.utcnow = lambda: NOW
    baseline_path = args.source_backend / "experiments/hevc_workbench_optimization/baseline_service.py"
    if sha256(baseline_path) != BASELINE_SHA256:
        raise AssertionError("Captured baseline oracle source changed")
    from experiments.hevc_workbench_optimization import baseline_service as baseline
    from app.services import hevc_workbench as candidate
    from app.db.session import engine, SessionLocal
    from sqlalchemy.orm import Session
    import psycopg
    observer.check_gil("shared_modules_and_native_imports")
    if engine.dialect.name != "postgresql":
        raise AssertionError("This differential check must exercise actual PostgreSQL")
    observer.install(engine, Session)
    # Initialize dialect/server metadata outside the first observed service
    # call, and verify the server actually enforces our read-only contract.
    with engine.connect() as connection:
        transaction_read_only = connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()
        if transaction_read_only != "on":
            raise AssertionError("PostgreSQL transaction is not read-only")
    report = {"mode": args.mode, "fixed_utc": NOW.isoformat(), "database_name": url.database,
              "transaction_read_only": transaction_read_only,
              "boundary": "Complete service contexts plus parsed live query roundtrips; no HTTP or timing claims",
              "baseline_source_sha256": BASELINE_SHA256,
              "candidate_source_sha256": sha256(Path(candidate.__file__)),
              "runner_sha256": sha256(Path(__file__).resolve()),
              "shared_module_sha256": {name: sha256(Path(importlib.import_module(name).__file__)) for name in
                  ("app.services.hevc_pairing", "app.services.releases_view", "app.services.torrent_files_meta",
                   "app.services.torrent_qb_meta", "app.services.runtime_settings")},
              "runtime": {"python": sys.version, "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                          "xoptions": sys._xoptions, "psycopg_backend": psycopg.pq.__impl__, "libpq_version": psycopg.pq.version(),
                          "packages": {name: importlib.metadata.version(name) for name in ("psycopg", "SQLAlchemy")}},
              "comparison": "Exact values/types/order for complete nested contexts, including checked_at, views/view_urls and row order",
              "cases": [], "smoke_only": args.allow_other_runtime}
    observer.report = report
    expected_checked_at = datetime_fmt.as_utc_iso(NOW)
    def compare(name, params, derived_from=None):
        expected, before = observer.invoke(SessionLocal, baseline.list_hevc_workbench, params, name + "_baseline")
        actual, after = observer.invoke(SessionLocal, candidate.list_hevc_workbench, params, name + "_candidate")
        mismatch = differences(expected, actual)
        record = {"name": name, "params": params, "derived_from": derived_from,
                  "baseline_digest": context_digest(expected), "candidate_digest": context_digest(actual),
                  "rows": len(actual["rows"]), "changed_rows": len(actual["changed_rows"]),
                  "total": actual["total"], "page": actual["page"], "per_page": actual["per_page"],
                  "checked_at": actual["checked_at"], "baseline": before, "candidate": after,
                  "equal": not mismatch}
        report["cases"].append(record)
        if mismatch:
            raise AssertionError(json.dumps({"case": name, "differences": mismatch}, ensure_ascii=False))
        if actual["checked_at"] != expected_checked_at:
            raise AssertionError("Service clock is not fixed")
        if len(after["file_queries"]) != 1 or not after["file_queries"][0]["postgresql_case"]:
            raise AssertionError("Candidate did not exercise the PostgreSQL CASE projection")
        if before["reads"] != after["reads"]:
            raise AssertionError("Candidate changed service SQL statement count")
        return expected
    try:
        selected = [(name, params) for name, params in cases() if not args.case or name in args.case]
        if args.case and len(selected) != len(set(args.case)):
            raise ValueError("An unknown --case name was requested")
        for name, params in selected:
            context = compare(name, params)
            if not args.no_live_roundtrips:
                compare(name + "-live-query", live_params(context), derived_from=name)
        observer.check_gil("all_cases_complete")
        report.update(passed=True, total_cases=len(report["cases"]), service_calls=observer.calls,
                      writes=sum(case[kind]["writes"] for case in report["cases"] for kind in ("baseline", "candidate")),
                      flushes=sum(case[kind]["flushes"] for case in report["cases"] for kind in ("baseline", "candidate")),
                      commits=sum(case[kind]["commits"] for case in report["cases"] for kind in ("baseline", "candidate")),
                      gil_checkpoints=observer.gil_points)
        return report
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--source-backend", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("regular", "ft_on", "ft_off"), default="regular")
    parser.add_argument("--case", action="append", help="Optional named direct case; its live query roundtrip is included")
    parser.add_argument("--no-live-roundtrips", action="store_true")
    parser.add_argument("--allow-other-runtime", action="store_true")
    args = parser.parse_args()
    args.source_backend, args.media_root, args.output = (value.resolve() for value in
        (args.source_backend, args.media_root, args.output))
    if args.output.exists():
        parser.error("Refusing to overwrite existing differential output; choose a new path")
    observer = None
    try:
        observer = Observer(args)
        report = run(args, observer)
        status = 0
    except Exception as exc:
        report = dict(observer.report or {}) if observer else {}
        report.update(failed=True, passed=False, error=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc(), gil_checkpoints=observer.gil_points if observer else [],
                      last_call=observer.last_call if observer else None)
        status = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(args.output), "passed": report.get("passed", False),
                      "cases": report.get("total_cases"), "mode": args.mode}))
    raise SystemExit(status)


if __name__ == "__main__":
    main()
