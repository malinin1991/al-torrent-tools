#!/usr/bin/env python3
"""Controlled regular 3.15 / 3.15t GIL-on / 3.15t default GIL-off experiment.

Only local read-only operations are executed. No GIL-off override is ever set.
The runtime wrappers must select GIL-on or the free-threaded build's default.
Every child audits the actual GIL state after imports and after warmup/work.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import benchmark_runtime as common


MODES = ("regular", "ft_on", "ft_off")
ORDERS = ((0, 1, 2), (1, 2, 0), (2, 0, 1), (0, 2, 1), (2, 1, 0), (1, 0, 2))
BASE_CASES = ("startup", *common.WORKLOADS)
REFACTOR_CASES = (
    "hevc_refactor_w0", "hevc_cold_w1", "hevc_cold_w2", "hevc_cold_w4",
    "hevc_hot_w1", "hevc_hot_w2", "hevc_hot_w4",
    "hevc_snapshot_compute_w0", "hevc_snapshot_compute_w4",
)
CASES = (*BASE_CASES, *REFACTOR_CASES)
NOW = datetime(2026, 10, 13)
TIME_MARGIN = 0.03
RSS_MARGIN = 0.05


def cpu_constraints() -> dict[str, Any]:
    paths = (
        "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu.stat",
        "/sys/fs/cgroup/cpuset.cpus.effective", "/sys/fs/cgroup/cpuset.cpus",
        "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us", "/sys/fs/cgroup/cpuset/cpuset.cpus",
        "/proc/self/cgroup",
    )
    values = {}
    for path in paths:
        try:
            values[path] = Path(path).read_text().strip()
        except OSError:
            values[path] = None
    affinity = getattr(os, "sched_getaffinity", None)
    values["sched_affinity_cpus"] = sorted(affinity(0)) if affinity else None
    cpu_max = values["/sys/fs/cgroup/cpu.max"]
    if cpu_max:
        quota, period = cpu_max.split()
        values["quota_cores"] = None if quota == "max" else int(quota) / int(period)
    return values


def linux_memory() -> dict[str, int | None]:
    result = {"linux_vm_hwm_bytes": None, "linux_rss_after_work_bytes": None}
    try:
        lines = Path("/proc/self/status").read_text().splitlines()
    except OSError:
        return result
    names = {"VmHWM:": "linux_vm_hwm_bytes", "VmRSS:": "linux_rss_after_work_bytes"}
    for line in lines:
        fields = line.split()
        if fields and fields[0] in names:
            result[names[fields[0]]] = int(fields[1]) * 1024
    return result


def gil_state() -> bool:
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


def expected_gil(mode: str) -> bool:
    return mode != "ft_off"


def result_rows(found) -> list[tuple[Any, ...]]:
    return [(row.archive_id, row.status, row.age_hours, row.paired_hevc_archive_id)
            for row in found]


def load_case(name: str, fixture_dir: Path, backend: Path, releases_per_task: int):
    if name in common.WORKLOADS:
        return common.load_workload(name, fixture_dir, backend), lambda: None
    from scripts.hevc_parallel_experiment import evaluate_snapshot, snapshot_archives
    rows, keys = common.hevc_fixture()
    workers = int(name.rsplit("w", 1)[1])
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hevc-bench") \
        if name.startswith("hevc_hot_") else None
    snapshot = None if name.startswith("hevc_snapshot_compute_") \
        else snapshot_archives(rows, keys)

    def run():
        value = snapshot_archives(rows, keys) if snapshot is None else snapshot
        return result_rows(evaluate_snapshot(
            value, now=NOW, workers=workers, releases_per_task=releases_per_task,
            executor=pool,
        ))

    def close():
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    return run, close


def child(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(args.source_backend))
    initial = gil_state()
    build_free_threaded = bool(common.sysconfig.get_config_var("Py_GIL_DISABLED"))
    if initial != expected_gil(args.mode) or build_free_threaded != (args.mode != "regular"):
        print(json.dumps({"blocked": "initial_runtime_mode_mismatch", "gil_initial": initial,
                          "free_threaded_build": build_free_threaded}))
        return
    if args.child == "metadata":
        value = common.metadata()
        try:
            import psycopg.pq
        except ImportError as exc:
            value.update(blocked="common_psycopg_backend_unavailable", error=str(exc))
        else:
            value.update(psycopg_backend=psycopg.pq.__impl__, libpq_version=psycopg.pq.version())
        value.update(gil_initial=initial, gil_after_audit=gil_state())
        print(json.dumps(value, sort_keys=True))
        return
    if args.child == "startup":
        started = time.perf_counter()
        from app.services import hevc_pairing, file_hasher, matroska_attachments, release_checkpoint
        import jinja2
        elapsed = time.perf_counter() - started
        if gil_state() != expected_gil(args.mode):
            print(json.dumps({"blocked": "gil_changed_during_import", "gil_initial": initial,
                              "gil_after_import": gil_state()}))
            return
        print(json.dumps({"domain_import_sec": elapsed, "cpu_sec": time.process_time(),
                          "peak_rss_bytes": common.peak_rss_bytes(), "checksum": "imports-ok",
                          "gil_initial": initial, "gil_after_import": gil_state(),
                          "gil_after_work": gil_state(), "iterations": 1,
                          **linux_memory()}))
        return
    run, close = load_case(args.child, args.fixture_dir, args.source_backend, args.releases_per_task)
    try:
        after_import = gil_state()
        if after_import != expected_gil(args.mode):
            print(json.dumps({"blocked": "gil_changed_during_import", "gil_initial": initial,
                              "gil_after_import": gil_state()}))
            return
        for _ in range(3):
            result = run()
        after_warmup = gil_state()
        if after_warmup != expected_gil(args.mode):
            print(json.dumps({"blocked": "gil_changed_during_warmup", "gil_initial": initial,
                              "gil_after_warmup": gil_state()}))
            return
        checksum = common.digest(result)
        gc.collect()
        cpu_start, start = time.process_time(), time.perf_counter()
        for _ in range(args.iterations):
            result = run()
        elapsed, cpu = time.perf_counter() - start, time.process_time() - cpu_start
        if gil_state() != expected_gil(args.mode):
            print(json.dumps({"blocked": "gil_changed_during_work", "gil_initial": initial,
                              "gil_after_work": gil_state()}))
            return
        if common.digest(result) != checksum:
            raise RuntimeError("non-deterministic result")
        print(json.dumps({"elapsed_sec": elapsed, "cpu_sec": cpu,
                          "cpu_average_cores": cpu / elapsed, "cpu_percent": 100 * cpu / elapsed,
                          "operations_per_sec": args.iterations / elapsed,
                          "iterations": args.iterations, "result_count": len(result),
                          "peak_rss_bytes": common.peak_rss_bytes(), "checksum": checksum,
                          "gil_initial": initial, "gil_after_import": after_import,
                          "gil_after_warmup": after_warmup,
                          "gil_after_work": gil_state(), **linux_memory()}))
    finally:
        close()


def compare_samples(baseline: list[dict], candidate: list[dict], startup: bool = False) -> dict:
    fields = ("process_elapsed_sec", "cpu_sec", "peak_rss_bytes") if startup \
        else ("elapsed_sec", "cpu_sec", "peak_rss_bytes")
    result = {}
    for field in fields:
        a, b = [row[field] for row in baseline], [row[field] for row in candidate]
        result[field] = common.paired_comparison(a, b, RSS_MARGIN if field == "peak_rss_bytes" else TIME_MARGIN)
        if field == "peak_rss_bytes":
            result[field].update(common.paired_absolute_change(a, b))
        else:
            result[field + "_p95"] = common.paired_p95_comparison(a, b, TIME_MARGIN)
            if field != "cpu_sec":
                ratio = result[field]
                low, high = ratio["paired_bootstrap_95pct_ci"]
                ratio["throughput_speedup_median"] = 1 / ratio["candidate_over_baseline_median"]
                ratio["throughput_speedup_95pct_ci"] = [1 / high, 1 / low]
    if not startup:
        cores = common.paired_comparison([row["cpu_average_cores"] for row in baseline],
                                        [row["cpu_average_cores"] for row in candidate], TIME_MARGIN)
        cores.pop("verdict")
        cores.pop("allowed_noise_margin_pct")
        result["descriptive_cpu_core_ratio"] = cores
    return result


def controller(args: argparse.Namespace) -> None:
    runtimes = dict(value.split("=", 1) for value in args.runtime)
    if set(runtimes) != set(MODES) or any(not Path(path).is_absolute() for path in runtimes.values()):
        raise SystemExit("Supply exactly regular=/path, ft_on=/path and ft_off=/path wrappers")
    if args.repeats < 6 or args.repeats % 6 or args.warmups < 1:
        raise SystemExit("Repeats must be a positive multiple of 6 (minimum 6); warmups >= 1")
    cases = list(dict.fromkeys(args.case or CASES))
    report = {
        "schema_version": 1, "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": args.scope, "production_migration_authorized": False,
        "protocol": {
            "repeats": args.repeats, "warmups": args.warmups, "inner_warmups": 3,
            "target_sample_sec": args.target_sample_sec,
            "calibration": "regular pilots; same original HEVC iteration count in every refactor case",
            "mode_order": [list(MODES[index] for index in order) for order in ORDERS],
            "case_order": "seeded shuffle per complete A/B/C block",
            "time_margin_pct": 100 * TIME_MARGIN, "rss_margin_pct": 100 * RSS_MARGIN,
            "bootstrap": "10000 paired resamples; median ratio and recomputed p95 ratio",
            "threads": [0, 1, 2, 4], "releases_per_task": args.releases_per_task,
            "gil_off": "default only; no PYTHON_GIL=0 or -X gil=0; check after imports/warmup/work",
            "adaptive": "pilot duration and skip blocked modes/cases; fixed balanced repetition count, no repeat-until-pass",
            "cold_pool": "pool create and shutdown included in every evaluate call",
            "hot_pool": "one pool per child; initialized by warmup, reused across measured evaluations",
            "snapshot_compute": "snapshot creation + cold evaluation; excludes ORM/SQL/network",
            "rss": "child lifetime high-water includes imports/setup and may include fork/exec floor",
        },
        "driver": {"version": sys.version, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "common_driver_sha256": hashlib.sha256(Path(common.__file__).read_bytes()).hexdigest()},
        "host": {"platform": common.platform.platform(), "machine": common.platform.machine(),
                 "cpu_count": os.cpu_count(), "loadavg_before": list(os.getloadavg()),
                 "cpu_constraints_before": cpu_constraints()},
        "runtimes": {}, "samples": {mode: {case: [] for case in cases} for mode in MODES},
        "blocked": {}, "calibration": {},
        "limitations": [
            "The normalized Python psycopg/libpq backend is not parity with production psycopg-binary.",
            "Pure HEVC compute scaling is not end-to-end ALTT/API/worker acceleration.",
            "No Session/ORM object crosses a worker boundary; DB and external I/O are excluded.",
            "Hot pools and prebuilt snapshots are upper-bound compute scenarios, not current production behavior.",
            "BLAKE3 single-threaded local cached-file I/O is not Unraid array throughput or encoder speed.",
            "Small-sample p95 intervals may be inconclusive; no zero-slowdown claim from median alone.",
            "Native arm64 Linux results cannot establish target Unraid amd64 performance.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="altt-ft-bench-") as temporary:
        root = Path(temporary)
        backend = root / "backend"
        shutil.copytree(args.source_backend / "app", backend / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (backend / "scripts").mkdir()
        shutil.copy2(args.source_backend / "scripts/hevc_parallel_experiment.py",
                     backend / "scripts/hevc_parallel_experiment.py")
        manifest = {str(path.relative_to(backend)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(backend.rglob("*")) if path.is_file()}
        report["source_manifest"] = manifest
        report["source_manifest_sha256"] = common.digest(manifest)
        fixture_dir = root / "fixtures"
        fixture_dir.mkdir()
        report["fixtures"] = common.prepare_files(fixture_dir)
        report["fixtures"].update(hevc_releases=128, hevc_scenarios=8,
                                  chunks=math.ceil(128 / args.releases_per_task))
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP")
               if key in os.environ}
        env.update({"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_JIT": "0",
                    "DATABASE_URL": "sqlite:///:memory:", "PSYCOPG_IMPL": "python",
                    "TORRENT_STORAGE_DIR": str(root / "unused-torrents"),
                    "ANILIBRIA_MEDIA_ROOT": str(fixture_dir),
                    "ANILIBRIA_BASE_URL": "https://example.invalid/api/v1",
                    "ANILIBRIA_FALLBACK_BASE_URL": "https://example.invalid/api/v1"})

        def invoke(mode: str, case: str, iterations: int = 1) -> dict:
            command = [runtimes[mode], str(Path(__file__).resolve()), "--child", case,
                       "--mode", mode, "--source-backend", str(backend),
                       "--fixture-dir", str(fixture_dir), "--iterations", str(iterations),
                       "--releases-per-task", str(args.releases_per_task)]
            start = time.perf_counter()
            value = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=120)
            elapsed = time.perf_counter() - start
            if value.returncode:
                return {"blocked": "child_error", "returncode": value.returncode, "stderr": value.stderr[-4000:]}
            try:
                result = json.loads(value.stdout)
            except ValueError:
                return {"blocked": "invalid_child_json", "stderr": value.stderr[-4000:],
                        "stdout": value.stdout[-4000:]}
            result["process_elapsed_sec"] = elapsed
            if value.stderr:
                result["stderr"] = value.stderr[-4000:]
            return result

        active_modes = []
        for mode in MODES:
            meta = invoke(mode, "metadata")
            report["runtimes"][mode] = meta
            if "blocked" in meta:
                report["blocked"][mode] = meta
            elif mode == "ft_off" and meta["gil_after_audit"] and args.scope != "pure-compute":
                report["blocked"][mode] = {"blocked": "metadata_native_audit_reenabled_gil", "metadata": meta}
            else:
                active_modes.append(mode)
        packages = [report["runtimes"][mode]["packages"] for mode in active_modes]
        report["dependency_versions_identical"] = bool(packages) and all(p == packages[0] for p in packages)
        if not report["dependency_versions_identical"] or "regular" not in active_modes:
            report["overall_verdict"] = "blocked_dependency_or_regular_runtime_contract"
            save(args.output, report)
            return
        native = [{"stdlib": {name: {key: value for key, value in data.items() if key != "file"}
                              for name, data in report["runtimes"][mode]["stdlib_native_modules"].items()},
                   "pyyaml_native": report["runtimes"][mode]["pyyaml_native"],
                   "psycopg_backend": report["runtimes"][mode]["psycopg_backend"],
                   "libpq_version": report["runtimes"][mode]["libpq_version"]}
                  for mode in active_modes]
        report["native_environment_parity"] = all(value == native[0] for value in native)
        if not report["native_environment_parity"]:
            report["overall_verdict"] = "blocked_native_environment_mismatch"
            save(args.output, report)
            return
        # Calibration happens only on the regular runtime; refactor cases reuse its
        # original count, so every mode/thread count does the same total work.
        calibration_cases = list(dict.fromkeys(
            ["hevc_pairing"] + [case for case in cases if case in common.WORKLOADS]))
        for case in calibration_cases:
            pilot = invoke("regular", case)
            if "blocked" in pilot:
                report["blocked"]["calibration:" + case] = pilot
                report["overall_verdict"] = "blocked_calibration"
                save(args.output, report)
                return
            count = max(1, min(4000, math.ceil(args.target_sample_sec / pilot["elapsed_sec"])))
            report["calibration"][case] = {"iterations": count, "pilot": pilot}
        for case in cases:
            if case in REFACTOR_CASES:
                report["calibration"][case] = {"iterations": report["calibration"]["hevc_pairing"]["iterations"],
                                                "same_work_as": "hevc_pairing"}
        blocked_cases = set()
        for mode in active_modes:
            for case in cases:
                for _ in range(args.warmups):
                    result = invoke(mode, case, report["calibration"].get(case, {}).get("iterations", 1))
                    if "blocked" in result:
                        report["blocked"][mode + ":" + case] = result
                        blocked_cases.add((mode, case))
                        break
        rng = random.Random(315703)
        for repetition in range(args.repeats):
            shuffled = list(cases)
            rng.shuffle(shuffled)
            for case in shuffled:
                for index in ORDERS[repetition % len(ORDERS)]:
                    mode = MODES[index]
                    if mode not in active_modes or (mode, case) in blocked_cases:
                        continue
                    count = report["calibration"].get(case, {}).get("iterations", 1)
                    result = invoke(mode, case, count)
                    result["repetition"] = repetition
                    if "blocked" in result:
                        report["blocked"][mode + ":" + case] = result
                        blocked_cases.add((mode, case))
                    else:
                        report["samples"][mode][case].append(result)
            print(f"Measured balanced triplet {repetition + 1}/{args.repeats}", flush=True)
    report["host"]["loadavg_after"] = list(os.getloadavg())
    report["host"]["cpu_constraints_after"] = cpu_constraints()
    analyze(report, cases, args.repeats)
    save(args.output, report)


def analyze(report: dict, cases: list[str], repeats: int) -> None:
    report["summaries"], report["runtime_comparisons"], report["refactor_comparisons"] = {}, {}, {}
    checksums_ok = True
    for case in cases:
        report["summaries"][case] = {}
        checksums = set()
        for mode in MODES:
            samples = report["samples"][mode][case]
            checksums.update(row["checksum"] for row in samples)
            if len(samples) != repeats:
                continue
            fields = ("process_elapsed_sec", "domain_import_sec", "cpu_sec", "peak_rss_bytes") \
                if case == "startup" else ("elapsed_sec", "cpu_sec", "peak_rss_bytes",
                                           "cpu_average_cores", "cpu_percent", "operations_per_sec")
            fields += tuple(field for field in ("linux_vm_hwm_bytes", "linux_rss_after_work_bytes")
                            if all(row.get(field) is not None for row in samples))
            report["summaries"][case][mode] = {
                field: common.summary([row[field] for row in samples]) for field in fields}
        checksums_ok = checksums_ok and len(checksums) <= 1
        report["runtime_comparisons"][case] = {}
        for baseline, candidate in (("regular", "ft_on"), ("ft_on", "ft_off"), ("regular", "ft_off")):
            a, b = report["samples"][baseline][case], report["samples"][candidate][case]
            if len(a) == repeats and len(b) == repeats:
                report["runtime_comparisons"][case][baseline + "__" + candidate] = compare_samples(a, b, case == "startup")
    for mode in MODES:
        report["refactor_comparisons"][mode] = {}
        for baseline, candidate in (
            ("hevc_pairing", "hevc_refactor_w0"),
            *(("hevc_refactor_w0", case) for case in REFACTOR_CASES if case != "hevc_refactor_w0"),
            *((f"hevc_cold_w{workers}", f"hevc_hot_w{workers}") for workers in (1, 2, 4)),
            ("hevc_pairing", "hevc_snapshot_compute_w0"),
            ("hevc_pairing", "hevc_snapshot_compute_w4"),
        ):
            if baseline not in report["samples"][mode] or candidate not in report["samples"][mode]:
                continue
            a, b = report["samples"][mode][baseline], report["samples"][mode][candidate]
            if len(a) == repeats and len(b) == repeats:
                checksums_ok = checksums_ok and {row["checksum"] for row in a} == {row["checksum"] for row in b}
                report["refactor_comparisons"][mode][baseline + "__" + candidate] = compare_samples(a, b)
    report["workload_checksums_identical"] = checksums_ok
    report["overall_verdict"] = "blocked_output_mismatch" if not checksums_ok else \
        "partial_compute_only" if report["blocked"] else "experiment_completed_not_production_approval"


def save(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Saved {path}: {report['overall_verdict']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="append", default=[])
    parser.add_argument("--source-backend", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("runtime_benchmark_315t.json"))
    parser.add_argument("--scope", choices=("common-python-backend", "pure-compute"), default="common-python-backend")
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--target-sample-sec", type=float, default=0.15)
    parser.add_argument("--releases-per-task", type=int, default=32)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--child", choices=("metadata", *CASES))
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()
    if args.target_sample_sec <= 0 or args.releases_per_task < 1:
        parser.error("Sample duration and releases-per-task must be positive")
    if args.child:
        child(args)
    else:
        controller(args)


if __name__ == "__main__":
    main()
