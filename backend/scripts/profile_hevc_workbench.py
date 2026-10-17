#!/usr/bin/env python3
"""Read-only diagnostics for the original list_hevc_workbench service.

Run against the isolated 1024-release fixture. cProfile, line-anchored phase
tracing and optional tracemalloc are separate diagnostic passes. Their timing
and allocation overhead must never be reported as application benchmarks.
Original application source/functions are neither rewritten nor monkeypatched.

SQL execute wall/CPU includes the driver's execute/network/buffering boundary.
It excludes later cursor fetch, Python decoding and SQLAlchemy row hydration.
The archive/file phase includes execute PLUS result.all()/materialization;
the difference is labelled residual, not an independently timed DB fetch.
"""
from __future__ import annotations

import argparse
import ast
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
import cProfile
from datetime import datetime
import gc
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import pstats
import resource
import sys
import sysconfig
import threading
import time
import traceback
import textwrap


PHASE_SELECTORS = (
    ("inputs", "assignment", "current"),
    ("archive_query_and_materialization", "assignment", "archives"),
    ("file_query_and_materialization", "assignment", "files"),
    ("pairing_and_file_keys", "assignment", "needs"),
    ("active_archive_selection", "assignment", "active"),
    ("receipt_queries_and_materialization", "assignment", "receipts"),
    ("hash_job_query_and_scan", "assignment", "hash_jobs"),
    ("release_metadata_queries_and_scan", "assignment", "release_meta"),
    ("group_files_by_hash", "assignment", "by_hash"),
    ("build_workbench_rows", "assignment", "rows"),
    ("counts_filter_sort_and_pagination", "assignment", "counts"),
    ("visible_filesystem_probe", "assignment", "probes"),
    ("visible_pipeline_settings_and_site", "assignment", "checked_at"),
    ("visible_flags_and_links", "for_iterable", "visible"),
    ("output_context", "assignment", "changed"),
)
LOCAL = threading.local()


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gil_state():
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


class GILAudit:
    def __init__(self, args):
        self.args, self.points, self.lock = args, [], threading.Lock()
        self.check("process_start")

    def check(self, point):
        enabled = gil_state()
        with self.lock:
            self.points.append({"point": point, "gil": enabled, "thread_id": threading.get_ident()})
        if enabled != (self.args.mode != "ft_off"):
            raise RuntimeError(f"GIL changed or wrong mode at {point}; force-off is forbidden")
        if not self.args.allow_other_runtime:
            if sys.version_info[:2] != (3, 15) or bool(sysconfig.get_config_var("Py_GIL_DISABLED")) != (self.args.mode != "regular"):
                raise RuntimeError("Use the requested CPython 3.15 build, or --allow-other-runtime for local smoke only")


def setup(args, audit):
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("altt_minijinja", "altt_workbench")):
        raise ValueError("Only isolated PostgreSQL databases named altt_minijinja.../altt_workbench... are permitted")
    if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
        raise RuntimeError("No PYTHON_GIL=0 or -X gil=0")
    if not args.media_root.is_dir():
        raise ValueError("The isolated media fixture must already exist")
    os.environ.update(DATABASE_URL=args.database_url, ANILIBRIA_MEDIA_ROOT=str(args.media_root),
                      TORRENT_STORAGE_DIR=str(args.media_root / "torrents"), APP_ENV="test",
                      SECRET_KEY="hevc-profile-isolated", ANILIBRIA_BASE_URL="http://invalid.invalid",
                      ANILIBRIA_FALLBACK_BASE_URL="http://invalid.invalid", ANILIBRIA_ADMIN_BASE_URL="")
    sys.path.insert(0, str(args.source_backend))
    os.chdir(args.media_root)  # No checkout .env is read by Settings.
    from app.utils import datetime_fmt
    datetime_fmt.utcnow = lambda: args.now
    from app.db.session import engine, SessionLocal
    from app.services.hevc_workbench import list_hevc_workbench
    from sqlalchemy import event
    audit.check("application_and_native_imports")

    @event.listens_for(engine, "connect")
    def readonly(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET default_transaction_read_only = on")
        connection.commit()

    @event.listens_for(engine, "before_cursor_execute")
    def before(_connection, _cursor, statement, _params, context, _many):
        state = getattr(LOCAL, "state", None)
        read = statement.lstrip().upper().startswith(("SELECT", "SHOW", "EXPLAIN"))
        if not read:
            raise RuntimeError("Read-only profiling attempted non-read SQL: " + statement[:60])
        if state is not None:
            context._workbench_profile_start = (time.perf_counter(), time.thread_time())
            context._workbench_profile_phase = state.get("phase")
            context._workbench_profile_sql = statement

    @event.listens_for(engine, "after_cursor_execute")
    def after(_connection, cursor, _statement, _params, context, _many):
        state = getattr(LOCAL, "state", None)
        start = getattr(context, "_workbench_profile_start", None)
        if state is not None and start is not None:
            wall, cpu = time.perf_counter() - start[0], time.thread_time() - start[1]
            statement = context._workbench_profile_sql
            state["sql"].append({"phase": context._workbench_profile_phase, "execute_wall_sec": wall,
                                 "execute_thread_cpu_sec": cpu, "rowcount": cursor.rowcount,
                                 "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                                 "statement": statement, "parameters_recorded": False})

    @event.listens_for(engine, "commit")
    def commit(_connection):
        state = getattr(LOCAL, "state", None)
        if state is not None:
            state["commits"] += 1
            raise RuntimeError("Profiled service attempted a commit")

    engine.dispose()
    audit.check("read_only_engine_configured")
    return engine, SessionLocal, list_hevc_workbench


def phase_map(function):
    lines, first = inspect.getsourcelines(function)
    return phase_map_source("".join(lines), first, function.__name__)


def phase_map_source(source, first_line, function_name):
    """Locate declared top-level statements, independently of RHS formatting.

    Plain/annotated assignments and a top-level ``for row in visible`` are
    explicit selectors. A changed/missing/duplicate target fails closed rather
    than silently moving a boundary. Source line order is observed, so moving
    active classification ahead of the files query preserves phase identity.
    No AST node is compiled or substituted into the application.
    """
    source = textwrap.dedent(source)
    definitions = [node for node in ast.parse(source).body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name]
    if len(definitions) != 1:
        raise RuntimeError("Expected one original service definition for phase observation")
    body = definitions[0].body
    source_lines = source.splitlines()
    anchors = []
    for name, kind, target in PHASE_SELECTORS:
        matches = []
        for node in body:
            if kind == "assignment":
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                matched = any(isinstance(value, ast.Name) and value.id == target for value in targets)
            else:
                matched = (isinstance(node, (ast.For, ast.AsyncFor)) and
                           isinstance(node.iter, ast.Name) and node.iter.id == target and
                           isinstance(node.target, ast.Name) and node.target.id == "row")
            if matched:
                matches.append(node)
        if len(matches) != 1:
            raise RuntimeError(f"Phase {name!r} selector {kind}:{target} has {len(matches)} top-level matches; review changed source")
        node = matches[0]
        anchors.append({"phase": name, "line": first_line + node.lineno - 1,
                        "anchor": source_lines[node.lineno - 1].strip(),
                        "selector": {"kind": kind, "target": target}, "node_type": type(node).__name__,
                        "statement_ast_sha256": hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()})
    return sorted(anchors, key=lambda row: row["line"])


class PhaseTrace:
    """Observe only the original service frame; never transform its source."""
    def __init__(self, function, anchors, state):
        self.code, self.anchors, self.state = function.__code__, anchors, state
        self.line_numbers = [row["line"] for row in anchors]
        self.current, self.wall, self.cpu = None, None, None
        self.events = []

    def flush(self):
        if self.current is not None:
            self.events.append({"phase": self.current, "wall_sec": time.perf_counter() - self.wall,
                                "thread_cpu_sec": time.thread_time() - self.cpu})

    def trace(self, frame, event, _arg):
        if frame.f_code is not self.code:
            return None
        if event == "line":
            index = max(0, bisect_right(self.line_numbers, frame.f_lineno) - 1)
            phase = self.anchors[index]["phase"]
            if phase != self.current:
                self.flush()
                self.current = self.state["phase"] = phase
                self.wall, self.cpu = time.perf_counter(), time.thread_time()
        elif event == "return":
            self.flush()
            self.current = self.state["phase"] = None
        return self.trace


def context_summary(context):
    def serialize(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(type(value).__name__)
    content = json.dumps(context, ensure_ascii=False, sort_keys=True, default=serialize)
    return {"context_sha256": hashlib.sha256(content.encode()).hexdigest(), "context_bytes": len(content.encode()),
            "rows": len(context["rows"]), "changed_rows": len(context["changed_rows"]),
            "counts": context["counts"], "total": context["total"], "page": context["page"],
            "checked_count": context["checked_count"], "available_count": context["available_count"],
            "row_identities": [row["identity"] for row in context["rows"]]}


def current_memory():
    result = {"lifetime_peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024))}
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        result.update({name: int(fields[key].split()[0]) * 1024 for key, name in
                       (("VmRSS", "rss_bytes"), ("VmHWM", "vm_hwm_bytes"))})
    except (OSError, ValueError, KeyError):
        pass
    return result


def new_state(pass_name, request_index):
    return {"pass": pass_name, "request": request_index, "thread_id": threading.get_ident(),
            "phase": None, "sql": [], "commits": 0, "gc_events": [], "gc_starts": {}}


def gc_event(phase, info):
    state = getattr(LOCAL, "state", None)
    if state is None:
        return
    generation = info["generation"]
    if phase == "start":
        state["gc_starts"][generation] = (time.perf_counter(), state.get("phase"))
    elif phase == "stop":
        start = state["gc_starts"].pop(generation, None)
        if start is not None:
            state["gc_events"].append({"generation": generation, "phase": start[1],
                                       "wall_sec": time.perf_counter() - start[0],
                                       "collected": info.get("collected"), "uncollectable": info.get("uncollectable")})


def sql_summary(state):
    return {"queries": len(state["sql"]), "commits": state["commits"],
            "execute_wall_sec": sum(row["execute_wall_sec"] for row in state["sql"]),
            "execute_thread_cpu_sec": sum(row["execute_thread_cpu_sec"] for row in state["sql"]),
            "boundary": "DBAPI cursor.execute, including driver/network/buffering; excludes later fetch/decoding/SQLAlchemy hydration"}


def invoke(args, audit, factory, function, anchors, pass_name, index):
    state = new_state(pass_name, index)
    profiler = trace = None
    audit.check(pass_name + "_worker_start")
    with factory() as db:
        LOCAL.state = state
        start_wall, start_cpu = time.perf_counter(), time.thread_time()
        old_trace = sys.gettrace()
        try:
            if pass_name == "cprofile":
                profiler = cProfile.Profile(timer=time.thread_time if args.profile_clock == "thread_cpu" else time.perf_counter)
                profiler.enable()
            elif pass_name == "phases":
                if old_trace is not None:
                    raise RuntimeError("A debugger/trace hook is already active")
                trace = PhaseTrace(function, anchors, state)
                sys.settrace(trace.trace)
            context = function(db, now=args.now, view=args.view, search=args.search,
                               page=args.page, per_page=args.per_page)
        finally:
            if profiler is not None:
                profiler.disable()
            if trace is not None:
                sys.settrace(old_trace)
            wall, cpu = time.perf_counter() - start_wall, time.thread_time() - start_cpu
            LOCAL.state = None
    audit.check(pass_name + "_worker_finish")
    state.pop("gc_starts")
    state.update(diagnostic_wall_sec=wall, diagnostic_thread_cpu_sec=cpu,
                 result=context_summary(context), sql_summary=sql_summary(state))
    if trace is not None:
        state["phase_events"] = trace.events
        totals = {}
        for row in trace.events:
            group = totals.setdefault(row["phase"], {"wall_sec": 0, "thread_cpu_sec": 0})
            group["wall_sec"] += row["wall_sec"]
            group["thread_cpu_sec"] += row["thread_cpu_sec"]
        for name, group in totals.items():
            queries = [query for query in state["sql"] if query["phase"] == name]
            group["sql_execute_wall_sec"] = sum(query["execute_wall_sec"] for query in queries)
            group["sql_execute_thread_cpu_sec"] = sum(query["execute_thread_cpu_sec"] for query in queries)
            group["sql_queries"] = len(queries)
            group["outside_execute_wall_residual_sec"] = group["wall_sec"] - group["sql_execute_wall_sec"]
            group["outside_execute_residual_boundary"] = "Python assembly/compilation, fetch/decoding/hydration, allocations and tracing overhead; not pure DB fetch"
        state["phase_totals"] = totals
    return state, profiler


def profile_records(stats, top):
    functions = []
    for (filename, line, name), (primitive, calls, self_time, cumulative, _callers) in stats.stats.items():
        functions.append({"file": filename, "line": line, "function": name,
                          "primitive_calls": primitive, "calls": calls,
                          "self_sec": self_time, "cumulative_sec": cumulative})
    return {"total_calls": stats.total_calls, "primitive_calls": stats.prim_calls, "total_self_sec": stats.total_tt,
            "function_count": len(functions), "functions": functions,
            "top_cumulative": sorted(functions, key=lambda row: row["cumulative_sec"], reverse=True)[:top],
            "top_self": sorted(functions, key=lambda row: row["self_sec"], reverse=True)[:top]}


def run(args, audit):
    engine, factory, function = setup(args, audit)
    anchors = phase_map(function)
    source = Path(inspect.getsourcefile(function)).resolve()
    fixture_marker = args.media_root / ".altt_minijinja_fixture.json"
    report = {"diagnostic_only": True, "mode": args.mode, "concurrency": args.concurrency,
              "requests_per_pass": args.requests, "profile_clock": args.profile_clock,
              "phase_method": "AST-selected top-level assignments/visible loop sorted by actual source positions; sys.settrace observes only the original function, which is never rewritten",
              "phase_anchors": anchors, "source_file": str(source), "source_sha256": checksum(source),
              "function_source_sha256": hashlib.sha256(inspect.getsource(function).encode()).hexdigest(),
              "runner_sha256": checksum(Path(__file__).resolve()), "fixed_utc": args.now.isoformat(),
              "fixture_manifest": json.loads(fixture_marker.read_text()) if fixture_marker.exists() else None,
              "runtime": {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                          "gil": gil_state(), "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                          "config_args": sysconfig.get_config_var("CONFIG_ARGS"), "xoptions": sys._xoptions,
                          "jit": bool(getattr(getattr(sys, "_jit", None), "is_enabled", lambda: False)()),
                          "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}},
              "memory_before": current_memory(), "passes": {},
              "limits": ["All diagnostic timers include profiler/tracer/listener overhead; use a separate uninstrumented benchmark for performance conclusions.",
                         "cProfile uses the selected per-worker thread-CPU or wall timer; cumulative times can overlap across workers.",
                         "SQL execute is not total query/fetch/hydration time. Archive/file phase residual includes Python and instrument overhead.",
                         "GC events are observed only on the callback thread carrying this request; other worker pauses are not attributed. Counts/durations do not establish causality.",
                         "RSS/HWM is a whole-process diagnostic, including imports, warmup and result validation."]}
    expected = None
    profilers = []
    gc.callbacks.append(gc_event)
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="hevc-profile") as pool:
            barrier = threading.Barrier(args.concurrency)
            def warm(_index):
                barrier.wait()
                with factory() as db:
                    context = function(db, now=args.now, view=args.view, search=args.search,
                                       page=args.page, per_page=args.per_page)
                audit.check("warmup_worker")
                return {"thread_id": threading.get_ident(), "result": context_summary(context)}
            warmed = list(pool.map(warm, range(args.concurrency)))
            assert len({row["thread_id"] for row in warmed}) == args.concurrency
            expected = warmed[0]["result"]
            assert all(row["result"] == expected for row in warmed)
            report["warmup"] = warmed
            audit.check("after_warmup")
            for pass_name in args.passes:
                if pass_name == "allocations":
                    if args.concurrency != 1:
                        raise ValueError("tracemalloc allocations pass is restricted to C1")
                    import tracemalloc
                    tracemalloc.start(10)
                    before = tracemalloc.take_snapshot()
                values = list(pool.map(lambda index: invoke(args, audit, factory, function, anchors, pass_name, index), range(args.requests)))
                states = [value[0] for value in values]
                assert all(row["result"] == expected for row in states), "Context changed under profiling"
                report["passes"][pass_name] = states
                profilers.extend(value[1] for value in values if value[1] is not None)
                if pass_name == "allocations":
                    after = tracemalloc.take_snapshot()
                    current, peak = tracemalloc.get_traced_memory()
                    report["allocations"] = {"current_traced_bytes": current, "peak_traced_bytes": peak,
                        "snapshot_diff": [{"traceback": str(stat.traceback), "bytes_diff": stat.size_diff,
                                           "count_diff": stat.count_diff} for stat in after.compare_to(before, "traceback")[:args.top]],
                        "boundary": "retained allocations at end plus traced peak; temporary allocation churn is not fully described"}
                    tracemalloc.stop()
                audit.check("after_" + pass_name)
    finally:
        gc.callbacks.remove(gc_event)
        engine.dispose()
    if profilers:
        stats = pstats.Stats(profilers[0])
        for profiler in profilers[1:]:
            stats.add(profiler)
        args.prof_output.parent.mkdir(parents=True, exist_ok=True)
        stats.dump_stats(str(args.prof_output))
        report["cprofile"] = profile_records(stats, args.top)
        report["raw_profile"] = str(args.prof_output)
    audit.check("process_end")
    report.update(memory_after=current_memory(), result=expected, gil_checkpoints=audit.points,
                  smoke_only=args.allow_other_runtime)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--source-backend", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prof-output", type=Path)
    parser.add_argument("--mode", choices=("regular", "ft_on", "ft_off"), default="regular")
    parser.add_argument("--concurrency", type=int, choices=(1, 4), default=1)
    parser.add_argument("--requests", type=int)
    parser.add_argument("--passes", nargs="+", choices=("cprofile", "phases", "allocations"), default=["cprofile", "phases"])
    parser.add_argument("--profile-clock", choices=("thread_cpu", "wall"), default="thread_cpu")
    parser.add_argument("--now", type=datetime.fromisoformat, default=datetime(2026, 10, 17))
    parser.add_argument("--view", default="all")
    parser.add_argument("--search", default="")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--per-page", type=int, default=30)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--allow-other-runtime", action="store_true")
    args = parser.parse_args()
    args.source_backend, args.media_root, args.output = (value.resolve() for value in (args.source_backend, args.media_root, args.output))
    args.prof_output = args.prof_output.resolve() if args.prof_output else args.output.with_suffix(".prof")
    args.requests = args.requests if args.requests is not None else args.concurrency
    if args.output.exists() or ("cprofile" in args.passes and args.prof_output.exists()):
        parser.error("Refusing to overwrite an existing JSON or raw profile; choose fresh output paths")
    if "cprofile" in args.passes and args.output == args.prof_output:
        parser.error("JSON output and raw profile paths must differ")
    if args.requests < args.concurrency or args.now.tzinfo is not None or len(args.passes) != len(set(args.passes)):
        parser.error("Use requests >= concurrency, naive UTC --now, and distinct passes")
    audit = None
    try:
        audit = GILAudit(args)
        result = run(args, audit)
        status = 0
    except Exception as exc:
        result = {"failed": True, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                  "gil_checkpoints": audit.points if audit else [], "diagnostic_only": True}
        status = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(args.output), "failed": bool(result.get("failed")), "mode": args.mode,
                      "profile": result.get("raw_profile"), "diagnostic_only": True, "gil": gil_state()}))
    raise SystemExit(status)


if __name__ == "__main__":
    main()
