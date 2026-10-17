#!/usr/bin/env python3
"""Validate and summarize paired before/after batches; retain every outlier."""
from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path
import random
import re
import statistics


SEED = 315017
ROUNDS = 8
REQUESTS = 8
MODES = ("regular", "ft_off")
VARIANTS = ("baseline", "optimized")
PATTERN = re.compile(r"http-r(\d{2})-(regular|ft_off)-c([14])-(baseline|optimized)\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def quantile(values, fraction):
    ordered = sorted(values)
    location = (len(ordered) - 1) * fraction
    low = int(location)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (location - low)


def metrics(samples):
    latencies = [value for sample in samples for value in sample["latencies_sec"]]
    contexts = [(event["end"] - event["start"]) * 1000 for sample in samples
                for event in sample["measurement"]["events"] if event["kind"] == "context"]
    wall = sum(sample["wall_sec"] for sample in samples)
    cpu = sum(sample["cpu_sec"] for sample in samples)
    count = sum(sample["requests"] for sample in samples)
    return {
        "median_ms": statistics.median(latencies) * 1000,
        "p95_ms": quantile(latencies, .95) * 1000,
        "context_median_ms": statistics.median(contexts),
        "context_p95_ms": quantile(contexts, .95),
        "throughput_per_sec": count / wall,
        "cpu_ms_per_response": cpu / count * 1000,
        "cpu_average_cores": cpu / wall,
        "rss_after_mean_mib": statistics.mean(sample["memory_after"]["rss_bytes"] for sample in samples) / 1024**2,
        "peak_rss_max_mib": max(sample["memory_after"]["vm_hwm_bytes"] for sample in samples) / 1024**2,
    }


def compare(reference, candidate, draws=5000):
    require(set(reference) == set(candidate) == set(range(ROUNDS)), "Unpaired rounds")
    left = [reference[index] for index in range(ROUNDS)]
    right = [candidate[index] for index in range(ROUNDS)]
    ref, cand = metrics(left), metrics(right)
    ratios = {key: [] for key in ref}
    generator = random.Random(SEED)
    for _ in range(draws):
        chosen = [generator.randrange(ROUNDS) for _ in range(ROUNDS)]
        a, b = metrics([left[index] for index in chosen]), metrics([right[index] for index in chosen])
        for key in ratios:
            ratios[key].append(b[key] / a[key])
    return {key: {"reference": ref[key], "candidate": cand[key], "ratio": cand[key] / ref[key],
                  "change_percent": (cand[key] / ref[key] - 1) * 100,
                  "ci95_ratio": [quantile(ratios[key], .025), quantile(ratios[key], .975)]}
            for key in ref}


def validate_gil(data, mode, name):
    expected = mode == "regular"
    require(data.get("gil") is expected, "Final GIL mismatch: " + name)
    require(data.get("py_gil_disabled") in (0, 1)
            and bool(data["py_gil_disabled"]) == (mode == "ft_off"), "Interpreter build mismatch: " + name)
    points = data.get("process_gil_checkpoints", [])
    require(points and all(point["gil"] is expected for point in points), "Missing/invalid GIL checkpoints: " + name)
    require(not data.get("failed") and not data.get("smoke_only"), "Invalid child process: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-psycopg-implementation", choices=("python", "c"), default="python",
                        help="Require one driver contract throughout this separate series; defaults to the primary pure driver")
    parser.add_argument("--allow-source-difference", action="append", default=[],
                        help="Intentional production source difference; workbench service is allowed by default")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to replace an existing analysis")
    root = args.input.resolve()
    inputs = json.loads((root / "comparison-inputs.json").read_text())
    completion = json.loads((root / "comparison-complete.json").read_text())
    require(completion.get("source_hashes_verified_after_series") is True, "Sources were not rechecked after timing")
    allowed = {"app/services/hevc_workbench.py", *args.allow_source_difference}
    source_hashes = inputs["source_hashes"]
    source_differences = sorted(path for path in set(source_hashes["baseline"]) | set(source_hashes["optimized"])
                                if source_hashes["baseline"].get(path) != source_hashes["optimized"].get(path))
    unexpected = [path for path in source_differences if path.startswith(("app/", "scripts/", "experiments/")) and path not in allowed]
    require(not unexpected, "Unexpected implementation/harness differences: " + repr(unexpected))
    require(source_differences, "Before/after source snapshots have no recorded differences")

    provenance = {}
    for variant, mode in itertools.product(VARIANTS, MODES):
        name = f"provenance-{mode}-{variant}"
        data = json.loads((root / (name + ".json")).read_text())
        validate_gil(data, mode, name)
        require(data.get("packages"), "Missing package provenance: " + name)
        for path, sha256 in data["source_files"].items():
            require(source_hashes[variant].get(path) == sha256, "Source hash mismatch: " + name + " " + path)
        provenance[(variant, mode)] = data
    first = provenance[("baseline", "regular")]
    require(first["psycopg_backend"] == args.expected_psycopg_implementation,
            "Unexpected psycopg implementation for this series")
    require(all(data["packages"] == first["packages"] for data in provenance.values()), "Different package versions")
    require(all(data["psycopg_backend"] == first["psycopg_backend"]
                and data["libpq_version"] == first["libpq_version"] for data in provenance.values()),
            "Different psycopg/libpq contracts")
    for variant in VARIANTS:
        require(provenance[(variant, "regular")]["source_files"] == provenance[(variant, "ft_off")]["source_files"],
                "A variant changed sources between runtimes: " + variant)

    ledger = {}
    for line in (root / "commands-comparison.jsonl").read_text().splitlines():
        record = json.loads(line)
        require(record["name"] not in ledger, "Duplicate command record: " + record["name"])
        require(record["exit_code"] == 0 and record["clock_guard_passed"]
                and record["clock_gap_max_abs_sec"] <= 2, "Child/clock guard failed: " + record["name"])
        require(record["end_monotonic_sec"] >= record["start_monotonic_sec"], "Invalid monotonic clock record")
        require(record["image_id"] == inputs["image_ids"][record["mode"]], "Image changed during comparison")
        require(record["source"] == inputs["source_paths"][record["variant"]], "Ledger source/variant disagreement")
        ledger[record["name"]] = record
    require(all(f"provenance-{mode}-{variant}" in ledger for variant, mode in itertools.product(VARIANTS, MODES)),
            "Missing clock-checked provenance command records")
    groups, files, semantic_hashes, html_hashes = {}, [], set(), set()
    for path in sorted(root.glob("http-r*.json")):
        matched = PATTERN.fullmatch(path.stem)
        require(matched is not None, "Unexpected sample filename: " + path.name)
        round_text, mode, concurrency_text, variant = matched.groups()
        round_index, concurrency = int(round_text), int(concurrency_text)
        data = json.loads(path.read_text())
        validate_gil(data, mode, path.name)
        require(path.stem in ledger, "Sample has no clock-checked command record: " + path.name)
        require((data["kind"], data["engine"], data["mode"], data["concurrency"])
                == ("http", "current_jinja", mode, concurrency), "Sample metadata/filename disagreement: " + path.name)
        require(ledger[path.stem]["variant"] == variant and ledger[path.stem]["mode"] == mode, "Variant/ledger disagreement")
        require(data["url"] == inputs["protocol"]["url"], "Different HTTP request URL")
        require(data["requests"] == len(data["responses"]) == len(data["latencies_sec"]) == REQUESTS, "Incomplete request sample")
        require(data["latencies_sec"] == [response["elapsed_sec"] for response in data["responses"]]
                and {response["index"] for response in data["responses"]} == set(range(REQUESTS)),
                "Latency/index records disagree")
        require(all(math.isfinite(value) and value > 0 for value in
                    [data["wall_sec"], data["cpu_sec"], *data["latencies_sec"]]), "Invalid timing values")
        require(data["memory_after"].get("rss_bytes", 0) > 0 and data["memory_after"].get("vm_hwm_bytes", 0) > 0,
                "Missing Linux RSS evidence")
        for phase in ("warmup", "measurement"):
            counts = data[phase]["counts"]
            require({"sql_reads", "sql_writes", "commits"} <= counts.keys()
                    and counts["sql_reads"] > 0 and counts["sql_writes"] == counts["commits"] == 0,
                    "Missing SQL evidence or mutation during " + phase)
        for response in data["responses"]:
            require(response["status"] == 200 and response["semantic_sha256"] == data["expected_body"]["semantic_sha256"],
                    "HTTP/semantic mismatch: " + path.name)
            require(response["row_keys"] == data["expected_body"]["row_keys"], "Row identity/order changed")
            semantic_hashes.add(response["semantic_sha256"])
            html_hashes.add(response["html_sha256"])
        require(data["measurement"]["peaks"]["route"] == concurrency, "Requested HTTP concurrency was not observed")
        require(data["anyio_thread_tokens"] == 4 and len(set(data["warm_route_worker_ids"])) == 4,
                "The four-worker warmup contract changed")
        workers = {event["thread_id"] for event in data["measurement"]["events"] if event["kind"] == "context"}
        require(data["measurement"]["peaks"]["context"] == concurrency and len(workers) == concurrency,
                "Requested context-worker concurrency was not observed")
        for kind in ("route", "context", "render"):
            require(sum(event["kind"] == kind for event in data["measurement"]["events"]) == REQUESTS,
                    "Incomplete route/context/response instrumentation")
        require(all(math.isfinite(event["start"]) and math.isfinite(event["end"])
                    and event["end"] >= event["start"] for event in data["measurement"]["events"]),
                "Invalid instrumented event interval")
        key = (variant, mode, concurrency)
        require(round_index not in groups.setdefault(key, {}), "Duplicate measured round")
        groups[key][round_index] = data
        files.append(path.name)
    require(set(groups) == set(itertools.product(VARIANTS, MODES, (1, 4))), "Incomplete comparison matrix")
    require(set(completion["completed_commands"]) >= {Path(path).stem for path in files},
            "Completion record does not cover the measured samples")
    require(len(semantic_hashes) == 1 and len(html_hashes) == 1, "Before/after output differs across sources/runtimes")
    summaries = []
    for (variant, mode, concurrency), samples in sorted(groups.items()):
        require(set(samples) == set(range(ROUNDS)), "Expected exactly eight rounds per configuration")
        values = [samples[index] for index in range(ROUNDS)]
        worker_counts = [len({event["thread_id"] for event in sample["measurement"]["events"] if event["kind"] == "context"})
                         for sample in values]
        summaries.append({"variant": variant, "mode": mode, "concurrency": concurrency,
                          "rounds": ROUNDS, "observations": sum(sample["requests"] for sample in values),
                          **metrics(values),
                          "sql_reads_per_sample": [sample["measurement"]["counts"].get("sql_reads", 0) for sample in values],
                          "context_worker_threads_min": min(worker_counts), "context_worker_threads_max": max(worker_counts),
                          "peak_context_concurrency": max(sample["measurement"]["peaks"]["context"] for sample in values)})
    comparisons = []
    def add(question, left, right, **labels):
        comparisons.append({"question": question, **labels, "statistics": compare(groups[left], groups[right])})
    for mode, concurrency in itertools.product(MODES, (1, 4)):
        add("Optimized / baseline, same runtime", ("baseline", mode, concurrency), ("optimized", mode, concurrency),
            mode=mode, concurrency=concurrency)
    for variant, concurrency in itertools.product(VARIANTS, (1, 4)):
        add("Free-threaded / regular, same source", (variant, "regular", concurrency), (variant, "ft_off", concurrency),
            variant=variant, concurrency=concurrency)
    for variant, mode in itertools.product(VARIANTS, MODES):
        add("Four clients / one, same source and runtime", (variant, mode, 1), (variant, mode, 4), variant=variant, mode=mode)
    for concurrency in (1, 4):
        add("Optimized free-threaded / baseline regular", ("baseline", "regular", concurrency), ("optimized", "ft_off", concurrency),
            concurrency=concurrency)
    result = {
        "method": {"point": "pooled latency median/p95; total requests / total batch wall; CPU ms per completed response",
                   "confidence": "5000 paired block bootstrap resamples of eight whole rounds", "seed": SEED,
                   "p95_limit": "64 HTTP observations per configuration; exploratory tail estimate",
                   "memory_limit": "Application-process RSS only; includes setup/warmup, excludes PostgreSQL",
                   "context_limit": "Context event wall time includes I/O, scheduling and instrumentation; pooled medians are not additive or isolated CPU measurements",
                   "outliers_removed": 0},
        "samples": len(files), "requests": sum(item["observations"] for item in summaries), "files": files,
        "source_differences": source_differences, "allowed_implementation_differences": sorted(allowed),
        "package_versions": first["packages"], "psycopg_backend": first["psycopg_backend"], "libpq_version": first["libpq_version"],
        "expected_psycopg_implementation": args.expected_psycopg_implementation,
        "source_files": {variant: provenance[(variant, "regular")]["source_files"] for variant in VARIANTS},
        "semantic_sha256": next(iter(semantic_hashes)), "html_sha256": next(iter(html_hashes)),
        "clock_gap_max_abs_sec": max(record["clock_gap_max_abs_sec"] for record in ledger.values()),
        "summaries": summaries, "comparisons": comparisons,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "samples": len(files), "groups": len(summaries), "comparisons": len(comparisons)}))


if __name__ == "__main__":
    main()
