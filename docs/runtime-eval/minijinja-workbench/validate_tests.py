#!/usr/bin/env python3
"""Run frozen workbench regressions sequentially in all three runtime modes.

The source bind is read-only; SQLite/media/test artifacts stay in container
/tmp. This driver does not use the benchmark database or network. It records
actual pytest collection/report counts and fails on any skip, error, failed
test, unexpected collection count, or effective-GIL change. Existing results
and logs are never overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time
import traceback


TEST_FILES = (
    "tests/test_hevc_workbench.py",
    "tests/test_hevc_workbench_ui.py",
    "tests/test_minijinja_workbench_experiment.py",
    "tests/test_ui_live_partials.py",
)
EXPECTED_COLLECTED = 70


def child(args):
    """Executed by the selected container interpreter, never by host pytest."""
    checkpoints = []
    result = {"mode": args.inside_mode, "test_files": TEST_FILES}
    output = args.inside_output.resolve()
    if output.exists():
        raise SystemExit("Refusing to replace an existing test result")
    expected_gil = args.inside_mode != "ft_off"

    def check(point):
        actual = bool(sys._is_gil_enabled())
        checkpoints.append({"point": point, "gil": actual})
        if actual != expected_gil:
            raise RuntimeError(f"GIL mismatch at {point}: expected {expected_gil}, got {actual}")

    started = time.perf_counter()
    try:
        if os.environ.get("PYTHON_GIL") == "0" or sys._xoptions.get("gil") == "0":
            raise RuntimeError("Forced GIL-off is forbidden")
        if sys.version_info[:2] != (3, 15):
            raise RuntimeError("The dedicated regression contract requires CPython 3.15")
        if bool(sysconfig.get_config_var("Py_GIL_DISABLED")) != (args.inside_mode != "regular"):
            raise RuntimeError("Unexpected regular/free-threaded interpreter build")
        sys.path.insert(0, str(args.source.resolve()))
        os.chdir("/tmp")  # Never discover a checkout .env file.
        check("before_imports")
        for module in ("pytest", "sqlite3", "sqlalchemy", "fastapi", "jinja2", "markupsafe",
                       "minijinja", "minijinja._lowlevel", "app.main"):
            importlib.import_module(module)
            check("after_import:" + module)
        import pytest

        class Counts:
            def __init__(self):
                self.collected = []
                self.passed, self.skipped, self.failed, self.errors = set(), set(), set(), set()

            def pytest_collection_finish(self, session):
                self.collected = [item.nodeid for item in session.items]
                check("after_collection")

            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_call(self, item):
                check("before_test:" + item.nodeid)
                yield
                check("after_test:" + item.nodeid)

            def pytest_runtest_logreport(self, report):
                if report.passed and report.when == "call":
                    self.passed.add(report.nodeid)
                if report.skipped:
                    self.skipped.add(report.nodeid)
                if report.failed:
                    (self.failed if report.when == "call" else self.errors).add(report.nodeid)

        counts = Counts()
        test_args = ["-q", "-p", "no:cacheprovider", "--basetemp=/tmp/pytest-workbench",
                     "--rootdir=" + str(args.source.resolve()),
                     *(str(args.source.resolve() / name) for name in TEST_FILES)]
        exit_code = int(pytest.main(test_args, plugins=[counts]))
        check("after_pytest")
        result.update(
            pytest_exit_code=exit_code, pytest_arguments=test_args,
            collected=len(counts.collected), passed=len(counts.passed),
            skipped=len(counts.skipped), failed=len(counts.failed), errors=len(counts.errors),
            collected_per_module=dict(Counter(node.split("::", 1)[0] for node in counts.collected)),
            passed_per_module=dict(Counter(node.split("::", 1)[0] for node in counts.passed)),
            failed_nodeids=sorted(counts.failed), error_nodeids=sorted(counts.errors),
            skipped_nodeids=sorted(counts.skipped),
        )
        if exit_code or len(counts.collected) != EXPECTED_COLLECTED:
            raise RuntimeError("Regression suite failed or collected an unexpected number of tests")
        if set(counts.collected) != counts.passed or counts.failed or counts.errors or counts.skipped:
            raise RuntimeError("Dedicated experiment requires every collected test to pass, with zero skips/errors")
        result["success"] = True
    except Exception as exc:
        result.update(success=False, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    result.update(
        python=sys.version, executable=sys.executable, xoptions=dict(sys._xoptions),
        py_gil_disabled=sysconfig.get_config_var("Py_GIL_DISABLED"), gil_checkpoints=checkpoints,
        elapsed_sec=time.perf_counter() - started,
        test_source_sha256={name: hashlib.sha256((args.source / name).read_bytes()).hexdigest()
                            for name in TEST_FILES},
        packages={name: importlib.metadata.version(name) for name in
                  ("pytest", "minijinja", "Jinja2", "MarkupSafe", "fastapi", "SQLAlchemy")},
    )
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result.get(key) for key in
                      ("mode", "success", "collected", "passed", "skipped", "failed", "errors", "error")}), flush=True)
    raise SystemExit(0 if result["success"] else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--regular-image", default="altt-minijinja-eval:regular-20261017")
    parser.add_argument("--ft-image", default="altt-minijinja-eval:ft-20261017")
    parser.add_argument("--inside-mode", choices=("regular", "ft_on", "ft_off"), help=argparse.SUPPRESS)
    parser.add_argument("--inside-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source = args.source.resolve()
    for name in TEST_FILES:
        if not (args.source / name).is_file():
            parser.error("Missing frozen regression source: " + name)
    if args.inside_mode:
        if args.inside_output is None:
            parser.error("Internal test mode requires an output path")
        return child(args)
    if args.output is None:
        parser.error("--output is required")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    logs = args.output / "logs"
    logs.mkdir(exist_ok=True)
    modes = ("regular", "ft_on", "ft_off")
    for mode in modes:
        if (args.output / f"tests-{mode}.json").exists() or (logs / f"tests-{mode}.log").exists():
            parser.error(f"Refusing to replace tests-{mode}; use a new output directory")
    for mode in modes:
        image = args.regular_image if mode == "regular" else args.ft_image
        image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
        command = [
            "docker", "run", "--rm", "--read-only", "--network", "none",
            "--label", "altt.minijinja-eval=20261017", "--tmpfs", "/tmp:rw,size=268435456",
            "--mount", f"type=bind,source={args.source},target=/app,readonly",
            "--mount", f"type=bind,source={Path(__file__).resolve()},target=/validate_tests.py,readonly",
            "--mount", f"type=bind,source={args.output},target=/output",
            "--workdir", "/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/app",
            "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "DATABASE_URL=sqlite://",
            "--env", "APP_ENV=test", "--env", "SECRET_KEY=minijinja-isolated-regressions",
            "--env", "ANILIBRIA_MEDIA_ROOT=/tmp/media", "--env", "TORRENT_STORAGE_DIR=/tmp/torrents",
            image, "/usr/local/bin/python",
        ]
        if mode == "ft_on":
            command.extend(["-X", "gil=1"])
        command.extend(["/validate_tests.py", "--source", "/app", "--inside-mode", mode,
                        "--inside-output", f"/output/tests-{mode}.json"])
        print("START regressions " + mode, flush=True)
        started = time.perf_counter()
        with (logs / f"tests-{mode}.log").open("x") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        record = {"mode": mode, "image": image, "image_id": image_id, "command": command,
                  "utc": datetime.now(timezone.utc).isoformat(), "exit_code": completed.returncode,
                  "process_wall_sec": time.perf_counter() - started}
        with (args.output / "commands-tests.jsonl").open("a") as ledger:
            ledger.write(json.dumps(record) + "\n")
        if completed.returncode:
            print((logs / f"tests-{mode}.log").read_text()[-6000:], flush=True)
            raise SystemExit("FAILED regressions " + mode)
        result = json.loads((args.output / f"tests-{mode}.json").read_text())
        if not result.get("success"):
            raise SystemExit("Missing successful regression result: " + mode)
        print(f"DONE {mode}: {result['passed']} passed, {result['skipped']} skipped", flush=True)


if __name__ == "__main__":
    main()
