#!/usr/bin/env python3
"""Separate optimized-source Jinja/MiniJinja HTTP series on the C driver.

Eight balanced rounds, eight requests, two engines, two runtimes and C1/C4.
The existing harness/source is mounted read-only. Images are required explicitly
and launched by resolved immutable ID. No fixture preparation or automatic
rerun is performed; all evidence has its own directory and ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from analyze import SEED, require, validate_gil
from run_comparison import HARNESS, REQUESTS, ROUNDS, URL, source_hashes, utc, wait_with_clock_guard


ENGINES = ("current_jinja", "minijinja")
MODES = ("regular", "ft_off")
INPUTS = "renderer-inputs.json"
LEDGER = "commands-renderer.jsonl"
COMPLETE = "renderer-complete.json"
PROVENANCE_COMPLETE = "renderer-provenance-complete.json"


def provenance_names():
    return {f"provenance-{mode}-{engine}" for mode, engine in itertools.product(MODES, ENGINES)}


def verify_provenances(root, hashes):
    """Identity is established outside HTTP timing, without changing harness."""
    records = {}
    for mode, engine in itertools.product(MODES, ENGINES):
        name = f"provenance-{mode}-{engine}"
        data = json.loads((root / (name + ".json")).read_text())
        validate_gil(data, mode, name)
        require(data.get("packages") and data.get("source_files"), "Missing provenance: " + name)
        require(data.get("psycopg_backend") == "c", "Renderer series requires the psycopg C implementation: " + name)
        for path, value in data["source_files"].items():
            require(hashes.get(path) == value, "Source/provenance disagreement: " + name + " " + path)
        records[(engine, mode)] = data
    first = records[(ENGINES[0], MODES[0])]
    require(all(data["packages"] == first["packages"] for data in records.values()), "Different installed distributions")
    require(all(data["source_files"] == first["source_files"] for data in records.values()), "Different source provenance")
    require(all(data["libpq_version"] == first["libpq_version"] for data in records.values()), "Different libpq versions")
    return records


def timing_plan():
    groups = list(itertools.product(MODES, (1, 4)))
    plan = []
    for round_index in range(ROUNDS):
        offset = round_index % len(groups)
        ordered = groups[offset:] + groups[:offset]
        if round_index >= len(groups):
            ordered.reverse()
        for mode, concurrency in ordered:
            engines = ENGINES if round_index % 2 == 0 else tuple(reversed(ENGINES))
            for engine in engines:
                name = f"http-r{round_index:02d}-{mode}-c{concurrency}-{engine}"
                plan.append((name, mode, engine, ["--child", "http", "--engine", engine,
                    "--concurrency", str(concurrency), "--requests", str(REQUESTS), "--url", URL]))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="One immutable optimized backend snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regular-image", required=True, help="Explicit regular CPython C-driver image")
    parser.add_argument("--ft-image", required=True, help="Explicit free-threaded CPython C-driver image")
    parser.add_argument("--stage", choices=("all", "provenance", "http"), default="all")
    parser.add_argument("--network", default="altt-workbench-opt-20261017")
    parser.add_argument("--media-volume", default="altt-workbench-opt-media-20261017")
    parser.add_argument("--database-url", default="postgresql+psycopg://postgres@altt-workbench-opt-pg-20261017:5432/altt_minijinja_workbench")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not (source / HARNESS).is_file() or not (source / "experiments/minijinja_workbench/prototype.py").is_file():
        parser.error("Source must include the unchanged workbench harness and renderer prototype")
    if any(source.rglob(".env*")):
        parser.error("Refusing a frozen snapshot containing .env files")
    database = urlsplit(args.database_url)
    if not database.scheme.startswith("postgresql") or not re.fullmatch(r"(?:altt_minijinja|altt_workbench)[A-Za-z0-9_]*", database.path.lstrip("/")):
        parser.error("Only an experiment-owned PostgreSQL database is permitted")
    require((ROUNDS, REQUESTS, SEED) == (8, 8, 315017), "Imported protocol constants changed")
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    images = {"regular": args.regular_image, "ft_off": args.ft_image}
    ids = {mode: subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
           for mode, image in images.items()}
    require(all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in ids.values()), "Missing immutable Docker image ID")
    hashes = source_hashes(source)
    inputs = {
        "protocol": {"rounds": ROUNDS, "requests_per_sample": REQUESTS, "url": URL,
                     "engines": list(ENGINES), "modes": list(MODES), "concurrency": [1, 4],
                     "seed": SEED, "expected_psycopg_backend": "c"},
        "source_path": str(source), "output_path": str(output), "source_hashes": hashes, "images": images, "image_ids": ids,
        "database_name": database.path.lstrip("/"), "network": args.network, "media_volume": args.media_volume,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "host_helpers_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("run_comparison.py", "analyze.py")},
        "identity_contract": "Per-mode/engine provenance, immutable image IDs and source mounts, source hash recheck; HTTP harness remains unchanged",
    }
    input_path = output / INPUTS
    if input_path.exists():
        if json.loads(input_path.read_text()) != inputs:
            parser.error("Existing renderer inputs differ; choose a fresh series directory")
    else:
        with input_path.open("x") as file:
            file.write(json.dumps(inputs, indent=2) + "\n")
    plan = []
    if args.stage in ("all", "provenance"):
        plan.extend((f"provenance-{mode}-{engine}", mode, engine, ["--child", "provenance", "--engine", engine])
                    for mode, engine in itertools.product(MODES, ENGINES))
    if args.stage in ("all", "http"):
        plan.extend(timing_plan())
    completion_path = output / (PROVENANCE_COMPLETE if args.stage == "provenance" else COMPLETE)
    if completion_path.exists():
        parser.error("Refusing to overwrite a completion record")
    ledger_path = output / LEDGER
    previous_names = [json.loads(line)["name"] for line in ledger_path.read_text().splitlines()] if ledger_path.exists() else []
    if len(previous_names) != len(set(previous_names)):
        parser.error("Duplicate command names in the existing ledger")
    for name, *_ in plan:
        if (output / (name + ".json")).exists() or (logs / (name + ".log")).exists() or name in previous_names:
            parser.error("Refusing to replace evidence for " + name)
    if args.stage == "http":
        if not (output / PROVENANCE_COMPLETE).exists():
            parser.error("Run the separate provenance stage before --stage http")
        verify_provenances(output, hashes)
    checked_provenance = args.stage == "http"
    for name, mode, engine, extra in plan:
        if name.startswith("http-") and not checked_provenance:
            verify_provenances(output, hashes)
            checked_provenance = True
        command = ["docker", "run", "--rm", "--read-only", "--network", args.network,
            "--label", "altt.workbench-renderer=20261017", "--tmpfs", "/tmp:rw,size=268435456",
            "--mount", f"type=bind,source={source},target=/app,readonly",
            "--mount", f"type=bind,source={output},target=/output",
            "--mount", f"type=volume,source={args.media_volume},target=/bench/media,readonly",
            "--workdir", "/app", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/app",
            "--env", "APP_ENV=test", "--env", f"DATABASE_URL={args.database_url}",
            "--env", "ANILIBRIA_MEDIA_ROOT=/bench/media", "--env", "ANILIBRIA_BASE_URL=http://invalid.invalid",
            "--env", "ANILIBRIA_FALLBACK_BASE_URL=http://invalid.invalid", ids[mode], "/usr/local/bin/python",
            "/app/" + HARNESS, "--mode", mode, "--database-url", args.database_url,
            "--media-root", "/bench/media", "--output", "/output/" + name + ".json", *extra]
        print("START " + name, flush=True)
        with (logs / (name + ".log")).open("x") as log:
            record = wait_with_clock_guard(command, log)
        record.update(name=name, engine=engine, mode=mode, source=str(source), image=images[mode],
                      image_id=ids[mode], command=command)
        with ledger_path.open("a") as file:
            file.write(json.dumps(record) + "\n")
        if record["exit_code"] or not record["clock_guard_passed"]:
            print((logs / (name + ".log")).read_text()[-6000:], flush=True)
            raise SystemExit("Child/clock failure; evidence retained, no automatic rerun: " + name)
        print(f"DONE {name} ({record['process_monotonic_sec']:.2f}s; clock gap {record['clock_gap_max_abs_sec']:.6f}s)", flush=True)
    verify_provenances(output, hashes)
    require(source_hashes(source) == hashes, "Frozen source changed during the renderer series")
    with completion_path.open("x") as file:
        file.write(json.dumps({"stage": args.stage, "source_hashes_verified_after_series": True,
            "completed_commands": [name for name, *_ in plan], "utc": utc(time.time())}, indent=2) + "\n")


if __name__ == "__main__":
    main()
