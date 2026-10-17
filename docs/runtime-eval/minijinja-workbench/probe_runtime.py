"""Fail-closed runtime/import audit and concurrent-render correctness smoke test."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import sysconfig
import warnings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--renders", type=int, default=256)
    parser.add_argument("--expect-gil", choices=("0", "1"))
    parser.add_argument("--import-module", action="append", default=[])
    parser.add_argument("--application-root", type=Path)
    args = parser.parse_args()
    if args.threads < 1 or args.renders < args.threads:
        parser.error("Require threads >= 1 and renders >= threads")
    if os.environ.get("PYTHON_GIL") is not None:
        parser.error("Remove PYTHON_GIL; do not mask automatic GIL fallback")
    if sys._xoptions.get("gil") == "0":
        parser.error("Forced -X gil=0 is forbidden")
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    expected_gil = (not free_threaded) if args.expect_gil is None else args.expect_gil == "1"
    if sys._xoptions.get("gil") == "1" and not expected_gil:
        parser.error("-X gil=1 disagrees with --expect-gil")
    if args.application_root:
        sys.path.insert(0, str(args.application_root.resolve()))
    record = {
        "python_version": sys.version,
        "python_executable": sys.executable,
        "python_config_args": sysconfig.get_config_var("CONFIG_ARGS"),
        "python_soabi": sysconfig.get_config_var("SOABI"),
        "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
        "gil_enabled_before_imports": sys._is_gil_enabled(),
        "expected_gil": expected_gil,
        "xoptions": dict(sys._xoptions),
        "platform": platform.platform(),
        "imports": [],
        "ok": False,
    }

    def check_gil(stage: str) -> None:
        if sys._is_gil_enabled() != expected_gil:
            raise RuntimeError(f"Unexpected effective GIL at {stage}: {sys._is_gil_enabled()}")

    try:
        check_gil("startup")
        modules = [
            "markupsafe", "jinja2", "minijinja", "minijinja._lowlevel",
            "psycopg", "fastapi", "starlette", *args.import_module,
        ]
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            for name in dict.fromkeys(modules):
                module = importlib.import_module(name)
                row = {"name": name, "file": getattr(module, "__file__", None), "gil_enabled": sys._is_gil_enabled()}
                record["imports"].append(row)
                check_gil(f"import {name}")
            record["import_warnings"] = [{"category": item.category.__name__, "message": str(item.message)} for item in seen]
        import jinja2
        import minijinja
        import psycopg
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import dataclass

        if importlib.metadata.version("minijinja") != "3.0.1":
            raise RuntimeError("Unexpected MiniJinja version")
        if psycopg.pq.__impl__ != "python":
            raise RuntimeError(f"Expected the matched pure psycopg backend, got {psycopg.pq.__impl__}")
        record["psycopg"] = {"implementation": psycopg.pq.__impl__, "libpq_version": psycopg.pq.version()}
        record["distributions"] = dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions()))
        lowlevel = importlib.import_module("minijinja._lowlevel")
        native_path = Path(lowlevel.__file__)
        record["native_extension"] = {"path": str(native_path), "sha256": hashlib.sha256(native_path.read_bytes()).hexdigest()}

        @dataclass(frozen=True)
        class Item:
            label: str
            count: int

        templates = {"probe.html": "<section>{{ marker }}</section>{% for row in rows %}<p>{{ row.label }}:{{ row.count|double }}</p>{% endfor %}"}

        def double(value: int) -> int:
            return value * 2

        mini = minijinja.Environment(templates=templates, filters={"double": double})
        jinja = jinja2.Environment(loader=jinja2.DictLoader(templates), autoescape=True)
        jinja.filters["double"] = double
        expected = []
        contexts = []
        for index in range(args.renders):
            context = {"marker": f"task-{index}<&>", "rows": [Item(f"label-{row}<&>", index + row) for row in range(16)]}
            contexts.append(context)
            expected.append(jinja.get_template("probe.html").render(**context))

        started = threading.Barrier(args.threads)
        thread_ids = set()
        thread_ids_lock = threading.Lock()

        def render(index: int) -> str:
            if index < args.threads:
                started.wait(timeout=30)
            with thread_ids_lock:
                thread_ids.add(threading.get_ident())
            result = mini.render_template("probe.html", **contexts[index])
            if result != expected[index]:
                raise AssertionError(f"Concurrent rendering output mismatch in task {index}")
            check_gil(f"render {index}")
            return hashlib.sha256(result.encode()).hexdigest()

        mini.render_template("probe.html", **contexts[0])
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            digests = list(pool.map(render, range(args.renders)))
        check_gil("after concurrent renders")
        record["correctness_smoke"] = {
            "threads": args.threads, "renders": args.renders,
            "observed_worker_threads": len(thread_ids),
            "unique_outputs": len(set(digests)),
            "shared_environment": True, "python_dataclass_attributes": True,
            "python_filter_callback": True, "escaped_against_jinja": True,
            "note": "Correctness smoke only; no performance or scaling claim.",
        }
        record["gil_enabled_after_imports_and_renders"] = sys._is_gil_enabled()
        record["ok"] = True
    except BaseException as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        record["gil_enabled_at_error"] = sys._is_gil_enabled()
        raise
    finally:
        payload = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
        print(payload, end="")


if __name__ == "__main__":
    main()
