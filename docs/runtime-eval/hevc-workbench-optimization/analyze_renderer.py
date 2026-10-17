#!/usr/bin/env python3
"""Strict analysis of the independent optimized-source C-driver renderer run."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import itertools
import json
import math
from pathlib import Path
import re

from analyze import SEED, compare, metrics, require, validate_gil
from run_comparison import REQUESTS, ROUNDS, URL
from run_renderer_comparison import (COMPLETE, ENGINES, INPUTS, LEDGER, MODES,
                                     provenance_names, timing_plan, verify_provenances)


PATTERN = re.compile(r"http-r(\d{2})-(regular|ft_off)-c([14])-(current_jinja|minijinja)\Z")


def validate_ledger(root, inputs, expected_samples):
    records = {}
    expected_order = [f"provenance-{mode}-{engine}" for mode, engine in itertools.product(MODES, ENGINES)]
    expected_order.extend(name for name, *_ in timing_plan())
    expected_names = provenance_names() | expected_samples
    previous_end = None
    for line in (root / LEDGER).read_text().splitlines():
        record = json.loads(line)
        name = record["name"]
        require(name in expected_names and name not in records, "Unexpected/duplicate renderer ledger command: " + name)
        mode, engine = record["mode"], record["engine"]
        require(mode in MODES and engine in ENGINES, "Invalid renderer identity in ledger")
        require(record["exit_code"] == 0 and record["clock_guard_passed"] is True
                and 0 <= record["clock_gap_max_abs_sec"] <= 2, "Clock/child failure: " + name)
        require(record["end_monotonic_sec"] >= record["start_monotonic_sec"], "Invalid monotonic interval")
        require(previous_end is None or record["start_monotonic_sec"] >= previous_end, "Child processes overlapped")
        previous_end = record["end_monotonic_sec"]
        require(math.isfinite(record["process_monotonic_sec"]) and record["process_monotonic_sec"] > 0,
                "Invalid child duration")
        require(record["image_id"] == inputs["image_ids"][mode] and record["image"] == inputs["images"][mode],
                "Image identity changed: " + name)
        require(record["source"] == inputs["source_path"], "Source identity changed: " + name)
        command = record["command"]
        require(inputs["image_ids"][mode] in command, "Docker command did not launch by immutable image ID")
        require(f"type=bind,source={inputs['source_path']},target=/app,readonly" in command,
                "Source mount is not the recorded immutable snapshot")
        require(f"type=bind,source={inputs['output_path']},target=/output" in command, "Output came from a different recorded series directory")
        require(f"type=volume,source={inputs['media_volume']},target=/bench/media,readonly" in command,
                "Media fixture was not mounted read-only")
        for flag, expected in (("--mode", mode), ("--engine", engine)):
            require(flag in command and command[command.index(flag) + 1] == expected, "Ledger command/identity mismatch")
        records[name] = record
    require(set(records) == expected_names, "Missing clock-checked provenance/timing commands")
    require(list(records) == expected_order, "Recorded command order differs from the predeclared balanced plan")
    return records


def validate_instrumentation(data, concurrency, name):
    counts = data["measurement"]["counts"]
    for phase in ("warmup", "measurement"):
        require({"sql_reads", "sql_writes", "commits"} <= set(data[phase]["counts"]), "Missing explicit SQL counters")
        require(data[phase]["counts"]["sql_reads"] > 0
                and data[phase]["counts"]["sql_writes"] == data[phase]["counts"]["commits"] == 0,
                "SQL mutation in " + phase + ": " + name)
    reads, warm_reads = counts.get("sql_reads", 0), data["warmup"]["counts"].get("sql_reads", 0)
    require(isinstance(reads, int) and isinstance(warm_reads, int) and reads > 0 and warm_reads > 0
            and reads % REQUESTS == 0 and reads // REQUESTS == warm_reads // 4 and warm_reads % 4 == 0,
            "Warm/measured SQL read counts disagree: " + name)
    require(data["anyio_thread_tokens"] == 4, "Changed worker capacity")
    warm_workers = {event["thread_id"] for event in data["warmup"]["events"] if event["kind"] == "context"}
    require(len(warm_workers) == 4 and warm_workers == set(data["warm_route_worker_ids"]), "Four worker threads were not warmed")
    require(data["measurement"]["peaks"]["route"] == data["measurement"]["peaks"]["context"] == concurrency,
            "Requested HTTP/context concurrency was not observed")
    events = data["measurement"]["events"]
    points = data["process_gil_checkpoints"]
    for kind in ("route", "context", "render"):
        selected = [event for event in events if event["kind"] == kind]
        require(len(selected) == REQUESTS, "Incomplete " + kind + " instrumentation")
        require(all(event["phase"] == "measure" and isinstance(event["thread_id"], int)
                    and math.isfinite(event["start"]) and math.isfinite(event["end"])
                    and event["end"] > event["start"] for event in selected), "Invalid phase/thread intervals")
        for suffix in ("enter", "exit"):
            checkpoints = [point for point in points if point["phase"] == "measure" and point["point"] == kind + "_" + suffix]
            require(Counter(point["thread_id"] for point in checkpoints) == Counter(event["thread_id"] for event in selected),
                    "Worker GIL checkpoint/event mismatch")
    worker_count = len({event["thread_id"] for event in events if event["kind"] == "context"})
    require(worker_count == concurrency, "Actual distinct context workers differ from requested concurrency")
    return reads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.input.resolve(), args.output.resolve()
    if output.exists():
        parser.error("Refusing to overwrite renderer analysis")
    require((ROUNDS, REQUESTS, SEED) == (8, 8, 315017), "Imported protocol/statistical constants changed")
    inputs = json.loads((root / INPUTS).read_text())
    protocol = inputs["protocol"]
    require((protocol["rounds"], protocol["requests_per_sample"], protocol["url"], protocol["seed"])
            == (ROUNDS, REQUESTS, URL, SEED), "Different predeclared renderer protocol")
    require(protocol["engines"] == list(ENGINES) and protocol["modes"] == list(MODES)
            and protocol["concurrency"] == [1, 4] and protocol["expected_psycopg_backend"] == "c", "Different renderer/driver matrix")
    completion = json.loads((root / COMPLETE).read_text())
    require(completion.get("source_hashes_verified_after_series") is True and completion["stage"] in ("all", "http"),
            "Sources were not rechecked after the complete timing series")
    provenance = verify_provenances(root, inputs["source_hashes"])
    first = provenance[("current_jinja", "regular")]
    planned = {name for name, *_ in timing_plan()}
    ledger = validate_ledger(root, inputs, planned)
    require(set(completion["completed_commands"]) >= planned, "Completion does not cover the full matrix")
    groups, files, semantics, reads = {}, [], set(), set()
    html_hashes = {engine: set() for engine in ENGINES}
    body_bytes = {engine: set() for engine in ENGINES}
    for path in sorted(root.glob("http-r*.json")):
        matched = PATTERN.fullmatch(path.stem)
        require(matched is not None, "Unexpected renderer sample name: " + path.name)
        rt, mode, ct, engine = matched.groups()
        round_index, concurrency = int(rt), int(ct)
        data = json.loads(path.read_text())
        validate_gil(data, mode, path.name)
        require(path.stem in ledger and (ledger[path.stem]["mode"], ledger[path.stem]["engine"]) == (mode, engine),
                "Sample/ledger identity disagreement")
        require((data["kind"], data["mode"], data["concurrency"], data["engine"], data["url"])
                == ("http", mode, concurrency, engine, URL), "Sample metadata/filename disagreement")
        require(data["requests"] == len(data["responses"]) == len(data["latencies_sec"]) == REQUESTS, "Incomplete HTTP sample")
        require(data["latencies_sec"] == [row["elapsed_sec"] for row in data["responses"]]
                and {row["index"] for row in data["responses"]} == set(range(REQUESTS)), "Invalid request indexes/latencies")
        require(all(math.isfinite(value) and value > 0 for value in [data["wall_sec"], data["cpu_sec"], *data["latencies_sec"]]),
                "Invalid timing observations")
        require(data["memory_after"].get("rss_bytes", 0) > 0 and data["memory_after"].get("vm_hwm_bytes", 0) > 0,
                "Missing Linux process RSS evidence")
        reads.add(validate_instrumentation(data, concurrency, path.name))
        expected = data["expected_body"]
        require(expected["row_count"] == len(expected["row_keys"]) == len(set(expected["row_keys"])) == 30,
                "Unexpected visible fixture rows")
        for row in data["responses"]:
            require(row["status"] == 200 and row["semantic_sha256"] == expected["semantic_sha256"]
                    and row["row_keys"] == expected["row_keys"] and row["row_count"] == 30,
                    "HTTP/DOM/row identity disagreement")
            require(row["html_sha256"] == expected["html_sha256"] and row["body_bytes"] == expected["body_bytes"],
                    "Output changed inside one static sample")
            semantics.add(row["semantic_sha256"])
            html_hashes[engine].add(row["html_sha256"])
            body_bytes[engine].add(row["body_bytes"])
        key = (engine, mode, concurrency)
        require(round_index not in groups.setdefault(key, {}), "Duplicate renderer round")
        groups[key][round_index] = data
        files.append(path.name)
    require(set(groups) == set(itertools.product(ENGINES, MODES, (1, 4))) and len(files) == 64, "Incomplete renderer matrix")
    require(len(semantics) == len(reads) == 1, "Different DOM or SQL read workload across engines/runtimes")
    require(all(len(html_hashes[engine]) == len(body_bytes[engine]) == 1 for engine in ENGINES),
            "HTML changed within one renderer; entity differences between engines are permitted")
    summaries = []
    for (engine, mode, concurrency), paired in sorted(groups.items()):
        require(set(paired) == set(range(ROUNDS)), "Expected exactly eight paired rounds")
        values = [paired[index] for index in range(ROUNDS)]
        workers = [len({event["thread_id"] for event in data["measurement"]["events"] if event["kind"] == "context"}) for data in values]
        summaries.append({"engine": engine, "mode": mode, "concurrency": concurrency, "rounds": ROUNDS,
            "observations": sum(data["requests"] for data in values), **metrics(values),
            "body_bytes": next(iter(body_bytes[engine])), "sql_reads_per_sample": next(iter(reads)),
            "context_worker_threads_min": min(workers), "context_worker_threads_max": max(workers),
            "peak_context_concurrency": max(data["measurement"]["peaks"]["context"] for data in values)})
    comparisons = []
    def add(question, left, right, **labels):
        comparisons.append({"question": question, **labels, "statistics": compare(groups[left], groups[right])})
    for mode, concurrency in itertools.product(MODES, (1, 4)):
        add("MiniJinja / Jinja, same optimized source and runtime", ("current_jinja", mode, concurrency), ("minijinja", mode, concurrency),
            mode=mode, concurrency=concurrency)
    for engine, concurrency in itertools.product(ENGINES, (1, 4)):
        add("Free-threaded / regular, same renderer", (engine, "regular", concurrency), (engine, "ft_off", concurrency),
            engine=engine, concurrency=concurrency)
    for engine, mode in itertools.product(ENGINES, MODES):
        add("Four clients / one, same renderer and runtime", (engine, mode, 1), (engine, mode, 4), engine=engine, mode=mode)
    for concurrency in (1, 4):
        add("Free-threaded MiniJinja / regular Jinja", ("current_jinja", "regular", concurrency), ("minijinja", "ft_off", concurrency),
            concurrency=concurrency)
    require(len(comparisons) == 14, "Incorrect renderer comparison count")
    result = {"method": {"point": "Pooled request median/p95; total completed requests / total batch wall; CPU ms/response",
        "confidence": "5000 paired block bootstrap resamples of eight whole rounds", "seed": SEED, "outliers_removed": 0,
        "p95_limit": "64 HTTP observations per configuration; exploratory tail estimate",
        "context_limit": "Supplementary original-service event durations (end-start), overlapping HTTP; not additive phase attribution",
        "memory_limit": "Whole application-process RSS/HWM after validation, including setup/warmup; PostgreSQL excluded",
        "identity": "Unchanged HTTP harness; per-mode/engine provenance plus immutable image IDs/source mounts and end-of-series source hash verification"},
        "samples": len(files), "requests": sum(row["observations"] for row in summaries), "files": files,
        "source_path": inputs["source_path"], "source_files": first["source_files"], "full_source_hashes": inputs["source_hashes"],
        "images": inputs["images"], "image_ids": inputs["image_ids"],
        "host_helpers_sha256": inputs["host_helpers_sha256"],
        "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "package_versions": first["packages"],
        "psycopg_backend": first["psycopg_backend"], "libpq_version": first["libpq_version"],
        "semantic_sha256": next(iter(semantics)), "html_sha256_by_engine": {engine: next(iter(value)) for engine, value in html_hashes.items()},
        "clock_gap_max_abs_sec": max(row["clock_gap_max_abs_sec"] for row in ledger.values()),
        "summaries": summaries, "comparisons": comparisons}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "samples": len(files), "groups": len(summaries), "comparisons": len(comparisons)}))


if __name__ == "__main__":
    main()
