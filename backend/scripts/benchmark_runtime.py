#!/usr/bin/env python3
"""Compare Python runtimes on deterministic, local ALTT operations.

Run from backend (no production services are used)::

    python scripts/benchmark_runtime.py \
        --runtime baseline=/path/to/venv314/bin/python \
        --runtime candidate=/path/to/venv315/bin/python \
        --output ../docs/runtime_benchmark_314_315.json

The controller uses only the standard library. Children use the actual app
functions, identical snapshotted sources, fixtures and dependency versions.
This is an interpreter/local workload benchmark, not a production load test.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time
from typing import Any


WORKLOADS = (
    "hevc_pairing",
    "release_json_fingerprint",
    "jinja_fragment",
    "matroska_attachments",
    "blake3_cached_file_io",
)
FIXTURE_VERSION = 1
TIME_NOISE_MARGIN = 0.03
RSS_MARGIN = 0.05


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def peak_rss_bytes() -> int:
    # macOS returns bytes; Linux and the other supported Unix runtimes use KiB.
    native = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(native if sys.platform == "darwin" else native * 1024)


def metadata() -> dict[str, Any]:
    gil_probe = getattr(sys, "_is_gil_enabled", None)
    jit_probe = getattr(getattr(sys, "_jit", None), "is_enabled", None)
    native_modules = {}
    for name in ("_decimal", "_ssl", "_hashlib", "_sqlite3", "_lzma", "_bz2"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            native_modules[name] = {"available": False}
        else:
            native_modules[name] = {
                "available": True, "file": getattr(module, "__file__", None),
                "libmpdec_version": getattr(module, "__libmpdec_version__", None),
                "openssl_version": getattr(module, "OPENSSL_VERSION", None),
                "sqlite_version": getattr(module, "sqlite_version", None),
            }
    try:
        import yaml
    except ImportError:
        yaml_native = None
    else:
        yaml_native = bool(getattr(yaml, "__with_libyaml__", False))
    return {
        "executable": sys.executable,
        "version": sys.version,
        "version_info": list(sys.version_info),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "compiler": platform.python_compiler(),
        "gil_enabled": gil_probe() if gil_probe else True,
        "jit_enabled": jit_probe() if jit_probe else False,
        "utf8_mode": sys.flags.utf8_mode,
        "pyyaml_native": yaml_native,
        "stdlib_native_modules": native_modules,
        "config": {
            key: sysconfig.get_config_var(key)
            for key in (
                "CONFIG_ARGS", "CC", "CFLAGS", "PY_CFLAGS", "LDFLAGS",
                "Py_GIL_DISABLED", "WITH_PYMALLOC", "PY_LTO", "PGO_PROF_USE_FLAG",
            )
        },
        "packages": dict(sorted(
            (dist.metadata["Name"].lower().replace("_", "-"), dist.version)
            for dist in importlib.metadata.distributions()
        )),
    }


def hevc_fixture() -> tuple[list[Any], dict[int, frozenset[str]]]:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from app.services.hevc_pairing import content_file_keys

    now = datetime(2026, 10, 13, 0, 0)
    rows = []
    keys = {}

    def add(rid: int, index: int, codec: str, episodes: str, *,
            hours: int = 48, rip: str = "WEBRip", active: bool = True,
            ignored: bool = False, video_count: int = 9) -> None:
        aid = rid * 100 + index
        rows.append(SimpleNamespace(
            id=aid, release_id=rid, torrent_id=aid,
            torrent_description=episodes,
            torrent_type=f"{rip} 1080p {codec}",
            quality_json={
                "type": {"value": rip}, "quality": {"value": "1080p"},
                "codec": {"label": codec, "value": f"x/{codec}"},
            },
            created_at=now - timedelta(hours=hours),
            api_created_at=now - timedelta(hours=hours),
            api_present=active, superseded=not active,
            info_hash=f"{aid:040x}", ignore_hevc=ignored,
        ))
        keys[aid] = content_file_keys([
            f"release-{rid}/episode-{n:02}.mkv" for n in range(1, video_count + 1)
        ] + ["folder/poster.jpg", "folder/._episode-01.mkv"])

    for rid in range(1, 129):
        scenario = rid % 8
        add(rid, 1, "AVC", "1-6", hours=120, active=False, video_count=6)
        if scenario == 0:  # Current exact pair.
            add(rid, 4, "AVC", "1-9")
            add(rid, 5, "HEVC", "1-9")
        elif scenario == 1:  # Pure missing, including old historical AVC.
            add(rid, 4, "AVC", "1-9")
        elif scenario == 2:  # Range expansion inherits an overdue anchor.
            add(rid, 2, "HEVC", "1-6", hours=120, video_count=6)
            add(rid, 3, "AVC", "1-8", hours=70, active=False, video_count=8)
            add(rid, 4, "AVC", "1-9", hours=10)
        elif scenario == 3:  # WEB-DL/WEBRip mismatch has priority over overdue.
            add(rid, 3, "HEVC", "1-9", hours=80, rip="WEB-DL")
            add(rid, 4, "AVC", "1-9")
        elif scenario == 4:  # Same episodes, newer AVC reupload.
            add(rid, 3, "HEVC", "1-9", hours=80)
            add(rid, 4, "AVC", "1-9", hours=40)
        elif scenario == 5:  # Operator ignored the active missing AVC.
            add(rid, 4, "AVC", "1-9", ignored=True)
        elif scenario == 6:  # Covered historical range must not anchor a new file.
            add(rid, 2, "AVC", "1-8", hours=80, active=False, video_count=8)
            add(rid, 3, "HEVC", "1-8", hours=80, video_count=8)
            add(rid, 4, "AVC", "1-9", hours=10)
        else:  # Label-only rename: same video basenames, historical SLA anchor.
            add(rid, 2, "AVC", "1-8", hours=80, active=False)
            add(rid, 3, "HEVC", "1-8", hours=80)
            add(rid, 4, "AVC", "1-9", hours=10)
        add(rid, 10, "AVC", "OVA 1-2", rip="BDRip", hours=30)
        add(rid, 11, "HEVC", "OVA 1-2", rip="BDRip", hours=30)
    return rows, keys


def encode_ebml_size(size: int) -> bytes:
    for length in range(1, 9):
        if size < (1 << (length * 7)) - 1:
            return ((1 << (length * 7)) | size).to_bytes(length, "big")
    raise ValueError("fixture EBML element too large")


def ebml_element(element_id: int, content: bytes) -> bytes:
    encoded_id = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
    return encoded_id + encode_ebml_size(len(content)) + content


def prepare_files(directory: Path) -> dict[str, Any]:
    # Fixture generation is outside every measured subprocess.
    chunk = bytes(range(256)) * (1024 * 1024 // 256)
    media = directory / "cached-hash-input.bin"
    with media.open("wb") as handle:
        for _ in range(64):
            handle.write(chunk)
    attachments = b"".join(
        ebml_element(0x61A7,
            ebml_element(0x466E, f"font-{index:02}.ttf".encode())
            + ebml_element(0x4660, b"application/x-truetype-font")
            + ebml_element(0x465C, chunk[:65536]))
        for index in range(24)
    )
    mkv = directory / "attachments.mkv"
    mkv.write_bytes(ebml_element(0x1A45DFA3, b"")
                    + ebml_element(0x18538067, ebml_element(0x1941A469, attachments)))
    with media.open("rb") as handle:
        media_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "hash_file_bytes": media.stat().st_size,
        "hash_file_sha256": media_digest,
        "attachment_count": 24,
        "mkv_sha256": hashlib.sha256(mkv.read_bytes()).hexdigest(),
    }


def load_workload(name: str, fixture_dir: Path, backend: Path):
    if name == "hevc_pairing":
        from datetime import datetime
        from app.services.hevc_pairing import find_unpaired_avc
        rows, keys = hevc_fixture()
        def run():
            found = find_unpaired_avc(
                rows, now=datetime(2026, 10, 13), file_keys_by_archive_id=keys,
            )
            return [(row.archive_id, row.status, row.age_hours,
                     row.paired_hevc_archive_id) for row in found]
        return run
    if name == "release_json_fingerprint":
        from app.services.release_checkpoint import torrents_fingerprint
        payload = json.dumps([
            {"id": index, "hash": f"{index:040x}",
             "updated_at": f"2026-10-{index % 12 + 1:02}T12:00:00Z",
             "description": f"Эпизоды 1-{index % 24 + 1}",
             "quality": {"type": "WEBRip", "codec": "AVC", "value": "1080p"}}
            for index in range(200)
        ], ensure_ascii=False)
        def run():
            return torrents_fingerprint(json.loads(payload))
        return run
    if name == "jinja_fragment":
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(loader=FileSystemLoader(backend / "app/templates"),
                          autoescape=select_autoescape(("html", "xml")))
        template = env.get_template("partials/ignore_hevc_toggle.html")
        def run():
            return "".join(template.render(archive_id=index, ignore_hevc=index % 3 == 0)
                           for index in range(400))
        return run
    if name == "matroska_attachments":
        from app.services.matroska_attachments import read_matroska_attachments
        def run():
            return read_matroska_attachments(fixture_dir / "attachments.mkv")
        return run
    if name == "blake3_cached_file_io":
        from app.services.file_hasher import hash_file_blake3
        def run():
            return hash_file_blake3(fixture_dir / "cached-hash-input.bin",
                                    chunk_size=4 * 1024 * 1024)
        return run
    raise ValueError(name)


def child(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(args.source_backend))
    if args.child == "metadata":
        print(json.dumps(metadata(), sort_keys=True))
        return
    if args.child == "startup":
        start = time.perf_counter()
        # Module imports only: no app lifespan, scheduler, HTTP or DB operation.
        from app.services import hevc_pairing, file_hasher, matroska_attachments
        from app.services import release_checkpoint
        import jinja2
        elapsed = time.perf_counter() - start
        print(json.dumps({"domain_import_sec": elapsed,
                          "cpu_sec": time.process_time(),
                          "peak_rss_bytes": peak_rss_bytes(), "checksum": "imports-ok"}))
        return
    run = load_workload(args.child, args.fixture_dir, args.source_backend)
    for _ in range(3):
        result = run()
    checksum = digest(result)
    gc.collect()
    if args.trace_allocations:
        import tracemalloc
        tracemalloc.start()
        result = run()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if digest(result) != checksum:
            raise RuntimeError("non-deterministic allocation probe")
        print(json.dumps({"checksum": checksum, "traced_current_bytes": current,
                          "traced_peak_bytes": peak, "peak_rss_bytes": peak_rss_bytes()}))
        return
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    for _ in range(args.iterations):
        result = run()
    elapsed = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    if digest(result) != checksum:
        raise RuntimeError("non-deterministic workload output")
    print(json.dumps({"elapsed_sec": elapsed, "cpu_sec": cpu,
                      "iterations": args.iterations,
                      "peak_rss_bytes": peak_rss_bytes(), "checksum": checksum}))


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def summary(values: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(values), "p95": quantile(values, 0.95),
        "min": min(values), "max": max(values),
        "cv_pct": 100 * statistics.stdev(values) / statistics.mean(values)
        if len(values) > 1 and statistics.mean(values) else 0,
    }


def paired_comparison(baseline: list[float], candidate: list[float],
                      margin: float) -> dict[str, Any]:
    ratios = [c / b for b, c in zip(baseline, candidate, strict=True)]
    rng = random.Random(315314)
    resampled = [statistics.median(rng.choices(ratios, k=len(ratios)))
                 for _ in range(10000)]
    low, high = quantile(resampled, 0.025), quantile(resampled, 0.975)
    if high <= 1 + margin:
        verdict = "non_regressing_within_declared_margin"
    elif low > 1 + margin:
        verdict = "regression"
    else:
        verdict = "inconclusive"
    return {
        "candidate_over_baseline_median": statistics.median(ratios),
        "change_pct": 100 * (statistics.median(ratios) - 1),
        "paired_bootstrap_95pct_ci": [low, high],
        "allowed_noise_margin_pct": 100 * margin,
        "verdict": verdict,
        "paired_ratios": ratios,
    }


def paired_p95_comparison(baseline: list[float], candidate: list[float],
                          margin: float) -> dict[str, Any]:
    """Bootstrap matching pair indices, then recompute each runtime's p95."""
    rng = random.Random(315314)
    count = len(baseline)
    resampled = []
    for _ in range(10000):
        indices = rng.choices(range(count), k=count)
        numerator = quantile([candidate[index] for index in indices], 0.95)
        denominator = quantile([baseline[index] for index in indices], 0.95)
        resampled.append(numerator / denominator)
    low, high = quantile(resampled, 0.025), quantile(resampled, 0.975)
    observed = quantile(candidate, 0.95) / quantile(baseline, 0.95)
    return {
        "candidate_over_baseline_p95": observed,
        "change_pct": 100 * (observed - 1),
        "paired_bootstrap_95pct_ci": [low, high],
        "allowed_noise_margin_pct": 100 * margin,
        "verdict": "non_regressing_within_declared_margin" if high <= 1 + margin
        else "regression" if low > 1 + margin else "inconclusive",
    }


def paired_absolute_change(baseline: list[float], candidate: list[float]) -> dict[str, Any]:
    differences = [c - b for b, c in zip(baseline, candidate, strict=True)]
    rng = random.Random(315314)
    resampled = [statistics.median(rng.choices(differences, k=len(differences)))
                 for _ in range(10000)]
    low, high = quantile(resampled, 0.025), quantile(resampled, 0.975)
    return {
        "median_delta_bytes": statistics.median(differences),
        "paired_bootstrap_95pct_ci_bytes": [low, high],
        "median_delta_mib": statistics.median(differences) / (1024 * 1024),
        "paired_bootstrap_95pct_ci_mib": [low / (1024 * 1024), high / (1024 * 1024)],
        "measurable_increase": low > 0,
    }


def aggregate_verdict(verdicts: list[str]) -> str:
    return "regression" if "regression" in verdicts else \
        "inconclusive" if "inconclusive" in verdicts else \
        "non_regressing_within_declared_margins"


def controller(args: argparse.Namespace) -> None:
    if not args.runtime:
        raise SystemExit("Supply at least one --runtime NAME=/absolute/python/path")
    if args.repeats < 8 or args.warmups < 1:
        raise SystemExit("At least 8 measured pairs and 1 warmup required")
    runtimes = []
    for value in args.runtime:
        name, separator, executable = value.partition("=")
        if not separator or not name or not Path(executable).is_absolute():
            raise SystemExit(f"Invalid runtime: {value!r}")
        runtimes.append((name, executable))
    if len({name for name, _ in runtimes}) != len(runtimes):
        raise SystemExit("Runtime names must be unique")
    workloads = list(dict.fromkeys(args.workload or ("startup", *WORKLOADS)))
    script = Path(__file__).resolve()
    report: dict[str, Any] = {
        "schema_version": 2, "fixture_version": FIXTURE_VERSION,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "driver": {"version": sys.version, "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()},
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "cpu_count": os.cpu_count(), "loadavg_before": list(os.getloadavg())},
        "protocol": {
            "repeats": args.repeats, "warmup_processes": args.warmups,
            "warmup_iterations_per_child": 3,
            "target_sample_sec": args.target_sample_sec,
            "subprocess_isolation": True,
            "order": "adjacent pairs; AB/BA alternation; seeded workload shuffle",
            "time_noise_margin_pct": 100 * TIME_NOISE_MARGIN,
            "rss_margin_pct": 100 * RSS_MARGIN,
            "confidence": "percentile bootstrap of paired median ratios, 10000 resamples",
            "p95_confidence": "paired-index bootstrap of runtime p95 ratios, 10000 resamples",
            "p95_gate_enabled": args.gate_p95,
            "workloads": workloads,
            "tracemalloc": "separate untimed subprocesses",
            "rss": "whole child lifetime high-water mark, including imports and fixtures",
            "startup": "new process and domain imports; warm filesystem cache",
            "source": "immutable app snapshot, no .env copied; sanitized child env",
        },
        "limitations": [
            "No production PostgreSQL, qBittorrent, Telegram, AniLiberty or Kensetsu operations.",
            "No assertion about real encoding, disk arrays, Unraid or external service latency.",
            "Cached 64 MiB local I/O is not cold-disk or multi-terabyte hashing throughput.",
            "Platform-specific local results do not establish target Unraid/Linux amd64 performance.",
            "Only identical dependency versions allow an interpreter comparison; native wheel builds may differ.",
            "Performance gates use declared noise margins; this is not proof of a literal zero slowdown.",
            "Peak RSS includes imports/setup and cannot be interpreted as incremental operation memory.",
            "HEVC uses SimpleNamespace metadata rows, not SQLAlchemy DB query/ORM hydration costs.",
            "Startup measures selected domain-module imports, not a live API/worker lifespan or readiness.",
        ],
        "runtimes": {}, "calibration": {}, "samples": {}, "allocations": {},
    }
    with tempfile.TemporaryDirectory(prefix="altt-runtime-benchmark-") as scratch:
        root = Path(scratch)
        snapshot = root / "backend"
        shutil.copytree(args.source_backend / "app", snapshot / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = {
            str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(snapshot.rglob("*")) if path.is_file()
        }
        report["source_manifest_sha256"] = digest(manifest)
        report["source_manifest"] = manifest
        fixture_dir = root / "fixtures"
        fixture_dir.mkdir()
        report["fixtures"] = prepare_files(fixture_dir)
        report["fixtures"].update({
            "hevc_release_count": 128, "hevc_scenarios": 8,
            "hevc_scenarios_description": [
                "exact_pair", "missing", "historical_overdue_anchor", "web_type_mismatch",
                "avc_reupload", "ignored", "new_video_file", "label_only_rename",
            ],
            "fingerprint_torrent_count": 200, "template_rows": 400,
        })
        child_env = {
            key: os.environ[key] for key in
            ("PATH", "SYSTEMROOT", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
            if key in os.environ
        }
        child_env.update({
            "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_JIT": "0", "DATABASE_URL": "sqlite:///:memory:",
            "ANILIBRIA_BASE_URL": "https://example.invalid/api/v1",
            "ANILIBRIA_FALLBACK_BASE_URL": "https://example.invalid/api/v1",
            "TORRENT_STORAGE_DIR": str(root / "unused-torrents"),
            "ANILIBRIA_MEDIA_ROOT": str(fixture_dir),
        })

        def invoke(executable: str, workload: str, iterations: int = 1,
                   trace: bool = False) -> dict[str, Any]:
            command = [executable, str(script), "--child", workload,
                       "--source-backend", str(snapshot), "--fixture-dir", str(fixture_dir),
                       "--iterations", str(iterations)]
            if trace:
                command.append("--trace-allocations")
            started = time.perf_counter()
            completed = subprocess.run(command, cwd=root, env=child_env,
                                       capture_output=True, text=True, check=False, timeout=180)
            process_elapsed = time.perf_counter() - started
            if completed.returncode:
                raise RuntimeError(f"{executable} {workload} failed:\n{completed.stderr}")
            value = json.loads(completed.stdout)
            value["process_elapsed_sec"] = process_elapsed
            return value

        for name, executable in runtimes:
            report["runtimes"][name] = invoke(executable, "metadata")
            report["samples"][name] = {key: [] for key in workloads}
            report["allocations"][name] = {}
        packages = [report["runtimes"][name]["packages"] for name, _ in runtimes]
        report["dependency_versions_identical"] = all(p == packages[0] for p in packages)
        native = []
        for name, _ in runtimes:
            runtime = report["runtimes"][name]
            native.append({
                "stdlib": {module: {key: value for key, value in data.items() if key != "file"}
                           for module, data in runtime["stdlib_native_modules"].items()},
                "pyyaml_native": runtime["pyyaml_native"],
            })
        report["native_environment_parity"] = all(value == native[0] for value in native)
        report["required_stdlib_native_modules_available"] = all(
            data["available"] for value in native for data in value["stdlib"].values()
        )
        if len(runtimes) > 1 and not report["dependency_versions_identical"]:
            report["comparison_verdict"] = "blocked_dependency_mismatch"
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            raise SystemExit("Dependency versions differ; provenance saved, no timing comparison performed")
        previous = json.loads(args.iterations_from.read_text()) if args.iterations_from else None
        if previous:
            if previous["source_manifest_sha256"] != report["source_manifest_sha256"]:
                raise SystemExit("Cannot reuse iteration counts: source manifest differs")
            if previous["fixture_version"] != FIXTURE_VERSION:
                raise SystemExit("Cannot reuse iteration counts: fixture version differs")
            report["protocol"]["iteration_counts_from_sha256"] = hashlib.sha256(
                args.iterations_from.read_bytes()).hexdigest()
        for workload in (key for key in workloads if key != "startup"):
            if previous:
                count = previous["calibration"][workload]["iterations"]
                report["calibration"][workload] = {"iterations": count, "reused": True}
            else:
                pilot = invoke(runtimes[0][1], workload)
                count = max(1, math.ceil(args.target_sample_sec / pilot["elapsed_sec"]))
                report["calibration"][workload] = {"iterations": count, "pilot": pilot}
            print(f"Calibrated {workload}: {count} iterations", flush=True)
        for warmup in range(args.warmups):
            for _, executable in runtimes:
                for workload in workloads:
                    invoke(executable, workload,
                           report["calibration"].get(workload, {}).get("iterations", 1))
            print(f"Warmup {warmup + 1}/{args.warmups}", flush=True)
        rng = random.Random(315)
        for pair_index in range(args.repeats):
            shuffled = list(workloads)
            rng.shuffle(shuffled)
            for workload in shuffled:
                order = runtimes if pair_index % 2 == 0 else list(reversed(runtimes))
                for name, executable in order:
                    count = report["calibration"].get(workload, {}).get("iterations", 1)
                    sample = invoke(executable, workload, count)
                    sample["pair_index"] = pair_index
                    report["samples"][name][workload].append(sample)
            print(f"Measured pair {pair_index + 1}/{args.repeats}", flush=True)
        for name, executable in runtimes:
            for workload in (key for key in workloads if key != "startup"):
                report["allocations"][name][workload] = invoke(executable, workload, trace=True)
    report["host"]["loadavg_after"] = list(os.getloadavg())
    report["summaries"] = {}
    checksums_ok = True
    for workload in workloads:
        all_checksums = {
            sample["checksum"] for name, _ in runtimes
            for sample in report["samples"][name][workload]
        }
        all_checksums.update(
            report["allocations"][name][workload]["checksum"]
            for name, _ in runtimes if workload != "startup"
        )
        checksums_ok = checksums_ok and len(all_checksums) == 1
        fields = ("process_elapsed_sec", "domain_import_sec", "cpu_sec", "peak_rss_bytes") \
            if workload == "startup" else ("elapsed_sec", "cpu_sec", "peak_rss_bytes")
        report["summaries"][workload] = {
            name: {field: summary([sample[field] for sample in report["samples"][name][workload]])
                   for field in fields}
            for name, _ in runtimes
        }
    report["workload_checksums_identical"] = checksums_ok
    if len(runtimes) == 2:
        baseline_name, candidate_name = runtimes[0][0], runtimes[1][0]
        report["comparison"] = {}
        for workload in workloads:
            baseline = report["samples"][baseline_name][workload]
            candidate = report["samples"][candidate_name][workload]
            fields = ("process_elapsed_sec", "cpu_sec", "peak_rss_bytes") \
                if workload == "startup" else ("elapsed_sec", "cpu_sec", "peak_rss_bytes")
            report["comparison"][workload] = {
                field: paired_comparison(
                    [sample[field] for sample in baseline], [sample[field] for sample in candidate],
                    RSS_MARGIN if field == "peak_rss_bytes" else TIME_NOISE_MARGIN,
                ) for field in fields
            }
            report["comparison"][workload]["peak_rss_bytes"].update(paired_absolute_change(
                [sample["peak_rss_bytes"] for sample in baseline],
                [sample["peak_rss_bytes"] for sample in candidate],
            ))
            if args.gate_p95:
                for field in (key for key in fields if key != "peak_rss_bytes"):
                    report["comparison"][workload][field + "_p95"] = paired_p95_comparison(
                        [sample[field] for sample in baseline],
                        [sample[field] for sample in candidate], TIME_NOISE_MARGIN,
                    )
        verdicts = [data["verdict"] for workload in report["comparison"].values()
                    for data in workload.values()]
        report["comparison_verdict"] = (
            "failed_output_mismatch" if not checksums_ok else
            "blocked_native_environment" if not report["native_environment_parity"]
            or not report["required_stdlib_native_modules_available"] else aggregate_verdict(verdicts)
        )
        report["gate_decisions"] = {
            "median_time": aggregate_verdict([
                data["verdict"] for comparisons in report["comparison"].values()
                for field, data in comparisons.items()
                if field != "peak_rss_bytes" and not field.endswith("_p95")
            ]),
            "p95_time": aggregate_verdict([
                data["verdict"] for comparisons in report["comparison"].values()
                for field, data in comparisons.items() if field.endswith("_p95")
            ]) if args.gate_p95 else "not_gated",
            "rss_within_5pct": aggregate_verdict([
                comparisons["peak_rss_bytes"]["verdict"]
                for comparisons in report["comparison"].values()
            ]),
            "any_measurable_rss_increase": any(
                comparisons["peak_rss_bytes"]["measurable_increase"]
                for comparisons in report["comparison"].values()
            ),
            "native_environment": "equivalent" if report["native_environment_parity"]
            and report["required_stdlib_native_modules_available"] else "blocked",
        }
    else:
        report["comparison_verdict"] = "baseline_only" if len(runtimes) == 1 \
            else "inspect_pairwise_manually"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Saved {args.output}: {report['comparison_verdict']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="append", help="NAME=/absolute/path/to/python")
    parser.add_argument("--source-backend", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("runtime_benchmark.json"))
    parser.add_argument("--repeats", type=int, default=16)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--target-sample-sec", type=float, default=0.25)
    parser.add_argument("--workload", action="append", choices=("startup", *WORKLOADS))
    parser.add_argument("--iterations-from", type=Path,
                        help="Reuse exact iteration counts from an earlier report with identical sources")
    parser.add_argument("--gate-p95", action="store_true",
                        help="Require p95 wall/CPU non-inferiority as well as the median gate")
    parser.add_argument("--child", choices=("metadata", "startup", *WORKLOADS))
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--trace-allocations", action="store_true")
    args = parser.parse_args()
    if args.target_sample_sec <= 0:
        parser.error("--target-sample-sec must be positive")
    if args.child:
        child(args)
    else:
        controller(args)


if __name__ == "__main__":
    main()
