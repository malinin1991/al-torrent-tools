#!/usr/bin/env python3
"""Sequential, source-paired HEVC HTTP comparison using the existing harness.

The disposable database/media fixture must already exist. Both variants use
current_jinja; variant identity belongs to source mounts, filenames and ledger,
never to a fabricated renderer name. No source, database, or media is modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit


ROUNDS = 8
REQUESTS = 8
HARNESS = "scripts/benchmark_minijinja_workbench.py"
URL = "/hevc-workbench/live?view=all&page=1&per_page=30"


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def source_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
            and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def wait_with_clock_guard(command, log):
    """Observe wall/monotonic divergence; keep an invalid run's evidence intact."""
    start_wall, start_mono = time.time(), time.monotonic()
    last_wall, last_mono = start_wall, start_mono
    maximum, observations = 0.0, 0
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    while True:
        try:
            exit_code = process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            exit_code = None
        wall, mono = time.time(), time.monotonic()
        maximum = max(maximum, abs((wall - last_wall) - (mono - last_mono)))
        observations += 1
        last_wall, last_mono = wall, mono
        if exit_code is not None:
            break
    total_gap = (wall - start_wall) - (mono - start_mono)
    maximum = max(maximum, abs(total_gap))
    return {
        "exit_code": exit_code, "start_utc": utc(start_wall), "end_utc": utc(wall),
        "start_unix_sec": start_wall, "end_unix_sec": wall,
        "start_monotonic_sec": start_mono, "end_monotonic_sec": mono,
        "process_wall_sec": wall - start_wall, "process_monotonic_sec": mono - start_mono,
        "clock_gap_total_sec": total_gap, "clock_gap_max_abs_sec": maximum,
        "clock_gap_observations": observations, "clock_guard_passed": maximum <= 2.0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--optimized-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("all", "provenance", "http"), default="all")
    parser.add_argument("--regular-image", default="altt-minijinja-eval:regular-20261017")
    parser.add_argument("--ft-image", default="altt-minijinja-eval:ft-20261017")
    parser.add_argument("--network", default="altt-workbench-opt-20261017")
    parser.add_argument("--media-volume", default="altt-workbench-opt-media-20261017")
    parser.add_argument("--database-url", default="postgresql+psycopg://postgres@altt-workbench-opt-pg-20261017:5432/altt_minijinja_workbench")
    args = parser.parse_args()
    sources = {"baseline": args.baseline_source.resolve(), "optimized": args.optimized_source.resolve()}
    if sources["baseline"] == sources["optimized"]:
        parser.error("Use distinct frozen before/after source directories")
    for variant, source in sources.items():
        if not (source / HARNESS).is_file():
            parser.error("Missing frozen harness in " + variant)
        if any(source.rglob(".env*")):
            parser.error("Frozen source must not contain .env files: " + variant)
    database = urlsplit(args.database_url)
    if not database.scheme.startswith("postgresql") or not re.fullmatch(r"altt_minijinja[a-zA-Z0-9_]*", database.path.lstrip("/")):
        parser.error("Only the experiment PostgreSQL database is permitted")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    images = {"regular": args.regular_image, "ft_off": args.ft_image}
    image_ids = {mode: subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
        for mode, image in images.items()}
    inputs = {
        "protocol": {"rounds": ROUNDS, "requests_per_sample": REQUESTS, "url": URL,
                     "engine": "current_jinja", "modes": ["regular", "ft_off"],
                     "variants": ["baseline", "optimized"], "concurrency": [1, 4]},
        "source_paths": {variant: str(source) for variant, source in sources.items()},
        "source_hashes": {variant: source_hashes(source) for variant, source in sources.items()},
        "images": images, "image_ids": image_ids,
        "network": args.network, "media_volume": args.media_volume,
        "database_name": database.path.lstrip("/"),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    input_path = output / "comparison-inputs.json"
    if input_path.exists():
        if json.loads(input_path.read_text()) != inputs:
            parser.error("Existing comparison inputs differ; use a new series directory")
    else:
        input_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2) + "\n")

    plan = []
    if args.stage in ("all", "provenance"):
        for variant, mode in itertools.product(sources, images):
            plan.append((f"provenance-{mode}-{variant}", variant, mode,
                         ["--child", "provenance", "--engine", "current_jinja"]))
    if args.stage in ("all", "http"):
        groups = list(itertools.product(images, (1, 4)))
        for round_index in range(ROUNDS):
            offset = round_index % len(groups)
            ordered = groups[offset:] + groups[:offset]
            if round_index >= len(groups):
                ordered = list(reversed(ordered))
            for mode, concurrency in ordered:
                variants = ["baseline", "optimized"] if round_index % 2 == 0 else ["optimized", "baseline"]
                for variant in variants:
                    plan.append((f"http-r{round_index:02d}-{mode}-c{concurrency}-{variant}", variant, mode,
                                 ["--child", "http", "--engine", "current_jinja", "--concurrency", str(concurrency),
                                  "--requests", str(REQUESTS), "--url", URL]))
    for name, _variant, _mode, _extra in plan:
        if (output / (name + ".json")).exists() or (logs / (name + ".log")).exists():
            parser.error("Refusing to overwrite " + name + "; choose a new output directory")
    completion_path = output / ("comparison-complete.json" if args.stage != "provenance" else "provenance-complete.json")
    if completion_path.exists():
        parser.error("Refusing to replace an existing completion record")
    if args.stage == "http" and any(not (output / f"provenance-{mode}-{variant}.json").exists()
                                    for variant, mode in itertools.product(sources, images)):
        parser.error("Run the provenance stage before timing")

    ledger = output / "commands-comparison.jsonl"
    for name, variant, mode, extra in plan:
        command = [
            "docker", "run", "--rm", "--read-only", "--network", args.network,
            "--label", "altt.workbench-optimization=20261017", "--tmpfs", "/tmp:rw,size=268435456",
            "--mount", f"type=bind,source={sources[variant]},target=/app,readonly",
            "--mount", f"type=bind,source={output},target=/output",
            "--mount", f"type=volume,source={args.media_volume},target=/bench/media,readonly",
            "--workdir", "/app", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/app",
            "--env", "APP_ENV=test", "--env", f"DATABASE_URL={args.database_url}",
            "--env", "ANILIBRIA_MEDIA_ROOT=/bench/media", "--env", "ANILIBRIA_BASE_URL=http://invalid.invalid",
            "--env", "ANILIBRIA_FALLBACK_BASE_URL=http://invalid.invalid", image_ids[mode], "/usr/local/bin/python",
            "/app/" + HARNESS, "--mode", mode, "--database-url", args.database_url,
            "--media-root", "/bench/media", "--output", "/output/" + name + ".json", *extra,
        ]
        print("START " + name, flush=True)
        with (logs / (name + ".log")).open("x") as log:
            record = wait_with_clock_guard(command, log)
        record.update(name=name, variant=variant, mode=mode, source=str(sources[variant]),
                      image=images[mode], image_id=image_ids[mode], command=command)
        with ledger.open("a") as file:
            file.write(json.dumps(record) + "\n")
        if record["exit_code"] or not record["clock_guard_passed"]:
            print((logs / (name + ".log")).read_text()[-6000:], flush=True)
            raise SystemExit(f"FAILED {name}: exit={record['exit_code']}, max clock gap={record['clock_gap_max_abs_sec']:.3f}s; no automatic rerun")
        print(f"DONE {name} ({record['process_monotonic_sec']:.2f}s; clock gap {record['clock_gap_max_abs_sec']:.6f}s)", flush=True)
    if {variant: source_hashes(source) for variant, source in sources.items()} != inputs["source_hashes"]:
        raise SystemExit("Frozen source changed during the series; evidence retained, do not analyze as a valid comparison")
    completion_path.write_text(json.dumps({"stage": args.stage, "source_hashes_verified_after_series": True,
                                          "completed_commands": [name for name, *_ in plan],
                                          "utc": utc(time.time())}, indent=2) + "\n")


if __name__ == "__main__":
    main()
