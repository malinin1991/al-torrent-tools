#!/usr/bin/env python3
"""Host-side sequential Docker driver for the opt-in workbench experiment.

Start the disposable PostgreSQL/network/media volume described in BUILD.md
first. The source directory must be a clean frozen backend copy without .env.
This driver never connects to production services or changes application code.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import subprocess
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("prepare", "validate", "http", "render", "rendercontrols"), required=True)
    parser.add_argument("--regular-image", default="altt-minijinja-eval:regular-20261017")
    parser.add_argument("--ft-image", default="altt-minijinja-eval:ft-20261017")
    parser.add_argument("--network", default="altt-minijinja-20261017")
    parser.add_argument("--media-volume", default="altt-minijinja-media-20261017")
    parser.add_argument("--database-url", default="postgresql+psycopg://postgres@altt-minijinja-pg-20261017:5432/altt_minijinja_workbench")
    parser.add_argument("--rounds", type=int, default=8)
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    if not (args.source / "scripts/benchmark_minijinja_workbench.py").is_file():
        parser.error("--source must contain the frozen experimental backend")
    if any(args.source.glob(".env*")):
        parser.error("Refusing a source snapshot containing .env files")
    if "/altt_minijinja" not in args.database_url:
        parser.error("Only experiment database names are permitted")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "logs").mkdir(exist_ok=True)
    images = {"regular": args.regular_image, "ft_on": args.ft_image, "ft_off": args.ft_image}
    ledger = args.output / f"commands-{args.stage}.jsonl"

    def run(mode: str, name: str, extra: list[str]) -> None:
        if (args.output / (name + ".json")).exists() or (args.output / "logs" / (name + ".log")).exists():
            raise SystemExit(f"Refusing to overwrite {name}; choose a new series output directory")
        image = images[mode]
        command = [
            "docker", "run", "--rm", "--read-only", "--network", args.network,
            "--label", "altt.minijinja-eval=20261017",
            "--tmpfs", "/tmp:rw,size=268435456",
            "--mount", f"type=bind,source={args.source},target=/app,readonly",
            "--mount", f"type=bind,source={args.output},target=/output",
            "--mount", f"type=volume,source={args.media_volume},target=/bench/media" + ("" if args.stage == "prepare" else ",readonly"),
            "--workdir", "/app", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONPATH=/app", "--env", "APP_ENV=test",
            "--env", f"DATABASE_URL={args.database_url}",
            "--env", "ANILIBRIA_MEDIA_ROOT=/bench/media",
            "--env", "ANILIBRIA_BASE_URL=http://invalid.invalid",
            "--env", "ANILIBRIA_FALLBACK_BASE_URL=http://invalid.invalid",
            image, "/usr/local/bin/python",
        ]
        if mode == "ft_on":
            command += ["-X", "gil=1"]
        command += [
            "/app/scripts/benchmark_minijinja_workbench.py", "--mode", mode,
            "--database-url", args.database_url, "--media-root", "/bench/media",
            "--output", "/output/" + name + ".json", *extra,
        ]
        start = time.perf_counter()
        record = {"utc": datetime.now(timezone.utc).isoformat(), "name": name, "mode": mode, "command": command}
        print(f"START {name}", flush=True)
        with (args.output / "logs" / (name + ".log")).open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        record.update(exit_code=result.returncode, process_wall_sec=time.perf_counter() - start)
        with ledger.open("a") as file:
            file.write(json.dumps(record) + "\n")
        if result.returncode:
            print((args.output / "logs" / (name + ".log")).read_text()[-6000:], flush=True)
            raise SystemExit(f"FAILED {name}: {result.returncode}")
        print(f"DONE {name} ({record['process_wall_sec']:.2f}s)", flush=True)

    if args.stage == "prepare":
        run("regular", "fixture", ["--prepare"])
        return
    if args.stage == "validate":
        for mode in ("regular", "ft_on", "ft_off"):
            run(mode, f"provenance-{mode}", ["--child", "provenance", "--engine", "minijinja"])
            run(mode, f"correctness-{mode}", ["--child", "correctness", "--engine", "minijinja"])
        return

    # Keep engine pairs adjacent and alternate AB/BA. The four groups occupy
    # every group position twice across eight rounds; no selected reruns.
    groups = list(itertools.product(("regular", "ft_off"), (1, 4)))
    for round_index in range(args.rounds):
        offset = round_index % len(groups)
        ordered = groups[offset:] + groups[:offset]
        if round_index >= len(groups):
            ordered = list(reversed(ordered))
        for mode, concurrency in ordered:
            engines = (["snapshot_jinja", "minijinja"] if args.stage == "rendercontrols"
                       else ["current_jinja", "minijinja"])
            if round_index % 2:
                engines.reverse()
            for engine in engines:
                kind = "render" if args.stage in ("render", "rendercontrols") else "http"
                requests = 64 if kind == "render" else 8
                name = f"{args.stage}-r{round_index:02d}-{mode}-c{concurrency}-{engine}"
                run(mode, name, [
                    "--child", kind, "--engine", engine,
                    "--concurrency", str(concurrency), "--requests", str(requests),
                ])


if __name__ == "__main__":
    main()
