#!/usr/bin/env python3
"""Summarize whole paired batches; retain every latency and every outlier."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics


def quantile(values, fraction):
    ordered = sorted(values)
    location = (len(ordered) - 1) * fraction
    low = int(location)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (location - low)


def metrics(samples):
    latencies = [value for sample in samples for value in sample["latencies_sec"]]
    wall = sum(sample["wall_sec"] for sample in samples)
    count = sum(sample["requests"] for sample in samples)
    return {
        "median_ms": statistics.median(latencies) * 1000,
        "p95_ms": quantile(latencies, .95) * 1000,
        "throughput_per_sec": count / wall,
        "cpu_average_cores": sum(sample["cpu_sec"] for sample in samples) / wall,
    }


def compare(reference, candidate, *, draws=5000):
    if set(reference) != set(candidate):
        raise ValueError("Unpaired rounds in comparison")
    rounds = sorted(reference)
    left = [reference[index] for index in rounds]
    right = [candidate[index] for index in rounds]
    ref, cand = metrics(left), metrics(right)
    ratios = {key: [] for key in ref}
    rng = random.Random(315003)
    for _ in range(draws):
        selected = [rng.randrange(len(rounds)) for _ in rounds]
        a = metrics([left[index] for index in selected])
        b = metrics([right[index] for index in selected])
        for key in ratios:
            ratios[key].append(b[key] / a[key])
    return {
        key: {"reference": ref[key], "candidate": cand[key],
              "ratio": cand[key] / ref[key], "change_percent": (cand[key] / ref[key] - 1) * 100,
              "ci95_ratio": [quantile(ratios[key], .025), quantile(ratios[key], .975)]}
        for key in ref
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    groups, files, semantics = {}, [], set()
    for path in sorted(args.input.glob("*-r??-*-c*-*.json")):
        data = json.loads(path.read_text())
        if data.get("failed") or data.get("smoke_only"):
            raise ValueError(f"Invalid sample {path}")
        stage, round_text, _mode, _concurrency, _engine = path.stem.split("-", 4)
        round_index = int(round_text[1:])
        if (_mode, int(_concurrency[1:]), _engine) != (data["mode"], data["concurrency"], data["engine"]):
            raise ValueError(f"Filename disagrees with sample metadata: {path}")
        if data["requests"] != len(data["responses"]) or data["requests"] != len(data["latencies_sec"]):
            raise ValueError(f"Incomplete request sample: {path}")
        if data["requests"] != (8 if stage == "http" else 64):
            raise ValueError(f"Unexpected predeclared request count: {path}")
        if any(response["semantic_sha256"] != data["expected_body"]["semantic_sha256"]
               or response["status"] != 200 for response in data["responses"]):
            raise ValueError(f"Invalid response or semantic mismatch: {path}")
        key = (stage, data["mode"], data["concurrency"], data["engine"])
        if round_index in groups.setdefault(key, {}):
            raise ValueError(f"Duplicate round {key} {round_index}")
        if any(point["gil"] != (data["mode"] != "ft_off") for point in data["process_gil_checkpoints"]):
            raise ValueError(f"GIL mismatch: {path}")
        if data["measurement"]["counts"].get("sql_writes", 0) or data["measurement"]["counts"].get("commits", 0):
            raise ValueError(f"Mutation during measurement: {path}")
        groups[key][round_index] = data
        semantics.add((stage, data["expected_body"]["semantic_sha256"]))
        files.append(path.name)
    if not groups:
        raise ValueError("No measured samples")
    stages = {key[0] for key in groups}
    for stage in stages:
        baseline = "snapshot_jinja" if stage == "rendercontrols" else "current_jinja"
        expected_groups = {(stage, mode, concurrency, engine)
                           for mode in ("regular", "ft_off") for concurrency in (1, 4)
                           for engine in (baseline, "minijinja")}
        if {key for key in groups if key[0] == stage} != expected_groups:
            raise ValueError(f"Incomplete matrix for stage {stage}")
        if len({value for category, value in semantics if category == stage}) != 1:
            raise ValueError(f"Compared HTML differs across engines/runtime in {stage}")
    summary = []
    for key, paired in sorted(groups.items()):
        stage, mode, concurrency, engine = key
        if set(paired) != set(range(8)):
            raise ValueError(f"Expected 8 rounds, got {len(paired)}: {key}")
        samples = list(paired.values())
        worker_counts = [len({event["thread_id"] for event in sample["measurement"]["events"]
                             if event["kind"] in ("context", "render")}) for sample in samples]
        summary.append({
            "stage": stage, "mode": mode, "concurrency": concurrency, "engine": engine,
            "rounds": len(samples), "observations": sum(sample["requests"] for sample in samples),
            **metrics(samples),
            "max_rss_mib": max(sample["memory_after"].get("rss_bytes", 0) for sample in samples) / 1024**2,
            "max_peak_rss_mib": max(sample["memory_after"].get("vm_hwm_bytes", 0) for sample in samples) / 1024**2,
            "peak_route_concurrency": max(sample["measurement"]["peaks"]["route"] for sample in samples),
            "peak_render_concurrency": max(sample["measurement"]["peaks"]["render"] for sample in samples),
            "distinct_worker_threads_per_process_min": min(worker_counts),
            "distinct_worker_threads_per_process_max": max(worker_counts),
            "sql_reads_per_sample": [sample["measurement"]["counts"].get("sql_reads", 0) for sample in samples],
        })
    comparisons = []
    for stage in ("http", "render", "rendercontrols"):
        baseline = "snapshot_jinja" if stage == "rendercontrols" else "current_jinja"
        for mode in ("regular", "ft_off"):
            for concurrency in (1, 4):
                left, right = (stage, mode, concurrency, baseline), (stage, mode, concurrency, "minijinja")
                if left in groups and right in groups:
                    comparisons.append({"question": f"MiniJinja vs {baseline}, same runtime", "stage": stage,
                                        "mode": mode, "concurrency": concurrency,
                                        "statistics": compare(groups[left], groups[right])})
        for engine in (baseline, "minijinja"):
            for concurrency in (1, 4):
                left, right = (stage, "regular", concurrency, engine), (stage, "ft_off", concurrency, engine)
                if left in groups and right in groups:
                    comparisons.append({"question": "Free-threaded vs regular, same engine", "stage": stage,
                                        "engine": engine, "concurrency": concurrency,
                                        "statistics": compare(groups[left], groups[right])})
        for mode in ("regular", "ft_off"):
            for engine in (baseline, "minijinja"):
                left, right = (stage, mode, 1, engine), (stage, mode, 4, engine)
                if left in groups and right in groups:
                    comparisons.append({"question": "Four clients vs one, same runtime and engine", "stage": stage,
                                        "mode": mode, "engine": engine,
                                        "statistics": compare(groups[left], groups[right])})
        for concurrency in (1, 4):
            left, right = (stage, "regular", concurrency, baseline), (stage, "ft_off", concurrency, "minijinja")
            if left in groups and right in groups:
                comparisons.append({"question": f"Candidate t-off MiniJinja vs regular {baseline}", "stage": stage,
                                    "concurrency": concurrency, "statistics": compare(groups[left], groups[right])})
    result = {
        "method": {"point": "pooled per-request median/p95; total requests / total batch wall throughput",
                   "confidence": "5000 paired block bootstrap resamples of whole rounds, seed 315003",
                   "p95_limit": "Exploratory: only 64 HTTP observations per configuration; no strict tail guarantee",
                   "outliers_removed": 0},
        "files": files, "summaries": summary, "comparisons": comparisons,
        "semantic_digests": sorted(semantics),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "samples": len(files), "groups": len(summary), "comparisons": len(comparisons)}))


if __name__ == "__main__":
    main()
