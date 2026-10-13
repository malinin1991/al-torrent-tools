#!/usr/bin/env python3
"""Independent missing-path check: original HEVC vs snapshot + persistent hot pool.

Run after the full experiment with --iterations-from its report. The earlier raw
data and measured driver are preserved; this adds snapshot cost to the hot path.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path

import benchmark_free_threading as parent


CASE = "hevc_snapshot_hot_w4"
PARENT_PATH = Path(parent.__file__).resolve()
original_load = parent.load_case
original_analyze = parent.analyze


def load_case(name, fixture_dir, backend, releases_per_task):
    if name != CASE:
        return original_load(name, fixture_dir, backend, releases_per_task)
    from scripts.hevc_parallel_experiment import evaluate_snapshot, snapshot_archives
    rows, keys = parent.common.hevc_fixture()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hevc-snapshot-hot-bench")

    def run():
        # Fresh snapshot on every call; only the executor is reused.
        snapshot = snapshot_archives(rows, keys)
        return parent.result_rows(evaluate_snapshot(
            snapshot, now=parent.NOW, workers=4,
            releases_per_task=releases_per_task, executor=pool,
        ))

    return run, lambda: pool.shutdown(wait=True, cancel_futures=True)


def analyze(report, cases, repeats):
    original_analyze(report, cases, repeats)
    report["driver"]["parent_driver_sha256"] = hashlib.sha256(PARENT_PATH.read_bytes()).hexdigest()
    report["protocol"]["additional_case"] = (
        "Independent predeclared missing path: fresh snapshot plus persistent hot4 pool; "
        "same frozen source and operation count as full experiment, no pooling of samples"
    )
    for mode in parent.MODES:
        baseline = report["samples"][mode]["hevc_pairing"]
        candidate = report["samples"][mode][CASE]
        if len(baseline) != repeats or len(candidate) != repeats:
            continue
        if {row["checksum"] for row in baseline} != {row["checksum"] for row in candidate}:
            report["workload_checksums_identical"] = False
            report["overall_verdict"] = "blocked_output_mismatch"
        report["refactor_comparisons"][mode]["hevc_pairing__" + CASE] = parent.compare_samples(
            baseline, candidate)


parent.CASES = ("hevc_pairing", CASE)
parent.REFACTOR_CASES = (CASE,)
parent.load_case = load_case
parent.analyze = analyze
# Child processes must enter this same adapter before parsing the added case.
parent.__file__ = __file__


if __name__ == "__main__":
    parent.main()
