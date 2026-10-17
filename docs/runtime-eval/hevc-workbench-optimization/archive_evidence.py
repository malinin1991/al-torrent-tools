#!/usr/bin/env python3
"""Archive completed workbench evidence, only after all timing/diagnostics stop.

No Docker, SQL, imports of application code, bootstrap, deletion, or traversal
outside explicit evidence subtrees is performed. Existing destinations are
refused. A failed archive leaves its new partial destination for inspection.
Expanded backend copies and old pilot snapshots are deliberately not selected.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zipfile


SERIES = {
    "comparison-python": ("python", "comparison-inputs.json", "commands-comparison.jsonl", "comparison-complete.json"),
    "comparison-c": ("c", "comparison-inputs.json", "commands-comparison.jsonl", "comparison-complete.json"),
    "renderer-c": ("c", "renderer-inputs.json", "commands-renderer.jsonl", "renderer-complete.json"),
}
MODES = ("regular", "ft_on", "ft_off")
DENIED_PARTS = {".git", ".ssh", ".aws", ".docker", ".codex", ".agents", ".cache", "__pycache__",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "data", "media", "torrents"}
DENIED_NAMES = {".ds_store", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "auth.json", "id_rsa", "id_ed25519"}
DENIED_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".mp3", ".flac", ".wav", ".torrent",
                   ".db", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".so", ".pyd", ".dylib", ".dll"}
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 10000, 256 * 1024**2, 2 * 1024**3
PURE_TOOL_WHEELS = {"setuptools-83.0.0-py3-none-any.whl", "wheel-0.46.3-py3-none-any.whl"}
RUNTIME_CACHE_DIRECTORIES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
READ_SHA256 = {}
ROOT_PATTERNS = ("*-commands.json", "*-commands.jsonl", "*-complete.json", "primary-series*.json*",
                 "*-analysis.json", "host-platform.json", "setup-*.json", "validation-*.json", "test-*.json", "*.log")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    require(name and path.parts and not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and not re.match(r"^[A-Za-z]:", name) and not any(ord(character) < 32 for character in name),
            "Unsafe archive member: " + name)
    require(not any(part.lower() in DENIED_PARTS or part.lower().startswith(".env") for part in path.parts),
            "Private/data/cache path refused: " + name)
    require(path.name.lower() not in DENIED_NAMES and path.suffix.lower() not in DENIED_SUFFIXES,
            "Private/media/database/native artifact refused: " + name)
    return path


def checked_read(path, root):
    require(path.resolve().is_relative_to(root) and not path.is_symlink(), "Evidence escaped its root: " + str(path))
    before = path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_FILE_BYTES, "Not a bounded regular file: " + str(path))
    data = path.read_bytes()
    after = path.stat()
    require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino),
            "Evidence changed while reading: " + str(path))
    require(len(data) == before.st_size, "Incomplete evidence read: " + str(path))
    checksum = sha(data)
    require(path not in READ_SHA256 or READ_SHA256[path] == checksum, "Evidence changed between reads: " + str(path))
    READ_SHA256[path] = checksum
    return data


def read_json(root, name):
    safe_name(name)
    return json.loads(checked_read(root / name, root))


def read_ledger(root, name):
    return [json.loads(line) for line in checked_read(root / name, root).decode().splitlines() if line.strip()]


def walk_files(root, relative, omitted):
    base = root / relative
    require(base.is_dir() and not base.is_symlink() and base.resolve().is_relative_to(root),
            "Missing/escaped evidence directory: " + relative)
    count = 0
    for directory, subdirectories, files in os.walk(base, followlinks=False):
        # Host helper imports may create caches after freezing. Prune them
        # without traversing/reading their contents; symlinks still fail closed.
        for name in tuple(subdirectories):
            if name.lower() in RUNTIME_CACHE_DIRECTORIES:
                path = Path(directory) / name
                member = path.relative_to(root).as_posix()
                require(not path.is_symlink(), "Symlink refused: " + member)
                subdirectories.remove(name)
                omitted.append({"name": member, "reason": "Generated runtime cache directory excluded without traversal"})
        for name in sorted(subdirectories + files):
            path = Path(directory) / name
            member = path.relative_to(root).as_posix()
            safe_name(member)
            require(not path.is_symlink(), "Symlink refused: " + member)
            count += 1
            require(count <= MAX_FILES, "Evidence subtree exceeds bounded file count")
        for name in sorted(files):
            path = Path(directory) / name
            require(path.is_file(), "Special evidence file refused: " + str(path))
            yield path, path.relative_to(root).as_posix()


def validate_series(root, name, specification):
    driver, inputs_name, ledger_name, complete_name = specification
    analysis_name = name + "-analysis.json"
    analysis, inputs = read_json(root, analysis_name), read_json(root, name + "/" + inputs_name)
    complete = read_json(root, name + "/" + complete_name)
    require(analysis.get("samples") == 64 and analysis.get("requests") == 512,
            "Analysis must cover 64 samples / 512 responses: " + name)
    require(analysis.get("psycopg_backend") == driver and len(analysis.get("summaries", [])) == 8,
            "Analysis driver/group contract differs: " + name)
    require(sum(group["observations"] for group in analysis["summaries"]) == 512,
            "Analysis observation count differs: " + name)
    require(complete.get("source_hashes_verified_after_series") is True and complete.get("stage") in ("all", "http"),
            "Missing immutable-source completion: " + name)
    variants = ("current_jinja", "minijinja") if name == "renderer-c" else ("baseline", "optimized")
    expected = {f"http-r{round_index:02d}-{mode}-c{concurrency}-{variant}.json"
                for round_index in range(8) for mode in ("regular", "ft_off")
                for concurrency in (1, 4) for variant in variants}
    actual = {path.name for path in (root / name).glob("http-r*.json")}
    require(actual == expected == set(analysis.get("files", [])), "Missing/extra HTTP sample: " + name)
    ledger = read_ledger(root, name + "/" + ledger_name)
    by_name = {row["name"]: row for row in ledger}
    require(len(by_name) == len(ledger) == 68, "Expected 64 HTTP + four provenance commands: " + name)
    expected_provenance = {f"provenance-{mode}-{variant}" for mode in ("regular", "ft_off") for variant in variants}
    require(set(by_name) == {Path(value).stem for value in expected} | expected_provenance,
            "Ledger does not cover exact matrix/provenance: " + name)
    provenance = []
    for mode in ("regular", "ft_off"):
        for variant in variants:
            data = read_json(root, f"{name}/provenance-{mode}-{variant}.json")
            require(data.get("packages") and data.get("source_files")
                    and data["psycopg_backend"] == driver and data["gil"] is (mode == "regular"),
                    "Missing/inconsistent runtime provenance: " + name)
            provenance.append(data)
    require(all(data["packages"] == provenance[0]["packages"] and data["libpq_version"] == provenance[0]["libpq_version"]
                for data in provenance), "Series package/libpq provenance differs: " + name)
    snapshot_hashes = {variant: {row["path"]: row["sha256"] for row in read_json(
        root, f"frozen/{variant}/source-manifest.json")["files"]} for variant in ("baseline", "optimized")}
    expected_source_hashes = snapshot_hashes["optimized"] if name == "renderer-c" else snapshot_hashes
    require(inputs["source_hashes"] == expected_source_hashes, "Series source inputs differ from frozen archived snapshots: " + name)
    require(set(complete["completed_commands"]) >= {Path(value).stem for value in expected}, "Incomplete series completion: " + name)
    for command in ledger:
        require(command["exit_code"] == 0 and command["clock_guard_passed"] is True
                and command["clock_gap_max_abs_sec"] <= 2, "Failed/interrupted command in completed series: " + name)
        require(command["image_id"] == inputs["image_ids"][command["mode"]], "Series image identity differs: " + name)
        require((root / name / "logs" / (command["name"] + ".log")).is_file(), "Missing per-command log: " + command["name"])
    observations = 0
    for filename in sorted(expected):
        sample = read_json(root, name + "/" + filename)
        require(sample["kind"] == "http" and sample["requests"] == len(sample["responses"]) == len(sample["latencies_sec"]) == 8,
                "Incomplete raw sample: " + filename)
        require(all(response["status"] == 200 for response in sample["responses"]), "Failed HTTP response: " + filename)
        for phase in ("warmup", "measurement"):
            require(sample[phase]["counts"]["sql_writes"] == sample[phase]["counts"]["commits"] == 0,
                    "Raw sample contains SQL mutations: " + filename)
        require(sample["gil"] is (sample["mode"] == "regular"), "Raw sample effective GIL differs: " + filename)
        observations += len(sample["responses"])
    return {"analysis": analysis_name, "samples": len(actual), "requests": observations, "psycopg_backend": driver}


def validation_receipts(root):
    commands = read_ledger(root, "test-commands.jsonl") + read_ledger(root, "c-validation-commands.jsonl")
    receipts = {"pytest": [], "differential": [], "renderer_correctness": []}
    for backend in ("python", "c"):
        for mode in ("regular", "ft_off"):
            stem = f"pytest-full-postgres-{mode}" + ("-c" if backend == "c" else "")
            name = "logs/" + stem + ".log"
            lines = checked_read(root / name, root).decode().splitlines()
            summaries = [line.strip("= ") for line in lines if re.search(r"\b\d+ passed\b", line) and " in " in line]
            require(summaries, "No actual pytest summary: " + name)
            counts = {key.rstrip("s"): int(value) for value, key in re.findall(
                r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b", summaries[-1])}
            require(counts.get("passed", 0) > 0 and not any(counts.get(key, 0) for key in ("failed", "error", "skipped")),
                    "Incomplete/failed/skipped full regression suite: " + name)
            matches = [row for row in commands if row.get("name") == stem or
                       (row.get("mode") == mode and row.get("psycopg_backend", "python") == backend and "pytest" in row.get("command", []))]
            require(len(matches) == 1 and matches[0]["exit_code"] == 0, "Missing unique successful test command: " + name)
            require(any(value.startswith("ALTT_TEST_POSTGRES_URL=postgresql+psycopg://") for value in matches[0]["command"]),
                    "Full-suite command did not enable its isolated PostgreSQL tests: " + name)
            receipts["pytest"].append({"mode": mode, "psycopg_backend": backend, "log": name,
                "summary": summaries[-1], "counts": counts, "exit_code": 0})
    for backend in ("python", "c"):
        for mode in MODES:
            name = f"results/differential-{mode}" + ("-c" if backend == "c" else "") + ".json"
            data = read_json(root, name)
            checked_read(root / "logs" / (Path(name).stem + ".log"), root)
            require(data.get("passed") is True and not data.get("smoke_only")
                    and data["total_cases"] == len(data["cases"]) == 40
                    and data["writes"] == data["flushes"] == data["commits"] == 0,
                    "Missing complete exact differential proof: " + name)
            require(all(case["equal"] is True and case["baseline_digest"] == case["candidate_digest"] for case in data["cases"]),
                    "Differential proof contains an unequal case: " + name)
            require(data["runtime"]["psycopg_backend"] == backend, "Differential backend mismatch: " + name)
            receipts["differential"].append({"file": name, "mode": mode, "psycopg_backend": backend,
                "passed": data["passed"], "cases": data["total_cases"], "service_calls": data["service_calls"],
                "writes": data["writes"], "flushes": data["flushes"], "commits": data["commits"]})
    for mode in MODES:
        name = f"renderer-correctness-c/correctness-{mode}.json"
        data = read_json(root, name)
        provenance = read_json(root, f"renderer-correctness-c/provenance-{mode}.json")
        require(data.get("passed") is True and not data.get("smoke_only") and len(data["cases"]) == 54
                and provenance["psycopg_backend"] == "c", "Missing renderer correctness proof: " + name)
        receipts["renderer_correctness"].append({"file": name, "mode": mode, "passed": True, "cases": len(data["cases"])})
        for kind in ("correctness", "provenance"):
            checked_read(root / f"renderer-correctness-c/logs/{kind}-{mode}.log", root)
    renderer_commands = read_ledger(root, "renderer-correctness-c/commands-validate.jsonl")
    require(len(renderer_commands) == 6 and all(row["exit_code"] == 0 for row in renderer_commands),
            "Incomplete renderer correctness command ledger")
    require(read_json(root, "c-validation-complete.json").get("success") is True, "C validation did not complete")
    receipts["totals"] = {"full_test_suites": len(receipts["pytest"]),
        "passed_tests_across_suites": sum(row["counts"]["passed"] for row in receipts["pytest"]),
        "exact_context_cases": sum(row["cases"] for row in receipts["differential"]),
        "service_calls": sum(row["service_calls"] for row in receipts["differential"]),
        "renderer_html_cases": sum(row["cases"] for row in receipts["renderer_correctness"])}
    return receipts


def validate_source_archive(data, manifest):
    expected = {row["path"]: row for row in manifest["files"]}
    require(len(expected) == len(manifest["files"]), "Duplicate frozen manifest paths")
    seen, byte_count, names = set(), 0, set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = safe_name(member.name)
            require(member.name not in names, "Duplicate frozen tar member: " + member.name)
            names.add(member.name)
            require(path.parts[0] == "backend" and (member.isdir() or member.isfile())
                    and not (member.issym() or member.islnk()), "Unsafe frozen source archive member")
            require(len(names) <= MAX_FILES and member.size <= MAX_FILE_BYTES, "Unbounded frozen source archive")
            if member.isfile():
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                require(relative in expected, "Frozen archive file absent from manifest: " + relative)
                content = archive.extractfile(member).read()
                byte_count += len(content)
                require(byte_count <= MAX_TOTAL_BYTES, "Unbounded expanded frozen source")
                require(len(content) == expected[relative]["bytes"] and sha(content) == expected[relative]["sha256"],
                        "Frozen archive/manifest mismatch: " + relative)
                seen.add(relative)
    require(seen == set(expected), "Frozen archive omits manifest files")


def select_files(root):
    selected, omitted = {}, []
    for path in root.iterdir():
        if path.name.lower().startswith(".env"):
            raise ValueError("Root .env path refused")
        if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in ROOT_PATTERNS):
            selected[path.name] = path
    for name in (*SERIES, "results", "renderer-correctness-c", "diagnostics", "logs", "host-tools"):
        for path, member in walk_files(root, name, omitted):
            selected[member] = path
    for variant in ("baseline", "optimized"):
        for filename in ("source-manifest.json", "source-backend.tar.gz"):
            member = f"frozen/{variant}/{filename}"
            selected[member] = root / member
    successful = "c-driver/attempt-04"
    offline = read_json(root, successful + "/offline-inputs.json")
    vendor_names = {item["name"] for item in offline} | {"psycopg_c-3.3.6.tar.gz"}
    for path, member in walk_files(root, successful, omitted):
        if path.suffix == ".whl" and path.name not in vendor_names:
            omitted.append({"name": member, "reason": "Native/unregistered built wheel not archived"})
            continue
        selected[member] = path
    for attempt in ("attempt-01-network-stall", "attempt-02", "attempt-03"):
        directory = "c-driver/" + attempt
        require((root / directory / "commands.jsonl").is_file(), "Missing failed-build ledger: " + directory)
        for path, member in walk_files(root, directory, omitted):
            if path.suffix in (".log", ".jsonl", ".py", ".md") or path.name in ("Dockerfile", "sources.json"):
                selected[f"c-driver/failed-attempts/{attempt}/{path.name}"] = path
    require(len(selected) <= MAX_FILES, "Too many selected evidence files")
    for name, path in selected.items():
        safe_name(name)
        require(path.is_file() and not path.is_symlink(), "Missing regular evidence file: " + name)
        require(path.suffix != ".whl" or (name.startswith(successful + "/") and path.name in PURE_TOOL_WHEELS),
                "Only explicitly checked pure build-tool wheels are permitted: " + name)
    return selected, omitted


def validate_build(root):
    prefix = "c-driver/attempt-04/"
    summary = read_json(root, prefix + "summary.json")
    require(summary.get("ok") is True, "C controls were not built successfully")
    probes = []
    for mode, expected in (("regular", True), ("ft", False)):
        probe = read_json(root, prefix + mode + "-runtime-probe.json")
        require(probe.get("ok") is True and probe["psycopg"]["implementation"] == "c"
                and probe["checkpoints"] and all(row["gil_enabled"] is expected for row in probe["checkpoints"]),
                "C control natural-GIL import proof is missing")
        require(all(value is None for value in probe["environment"].values()) and "gil" not in probe["xoptions"],
                "C control used a GIL/backend override")
        require(probe["psycopg"] == summary["modes"][mode]["psycopg"]
                and probe["packages"]["psycopg-c"] == "3.3.6", "C control provenance differs from build summary")
        probes.append(probe)
    require(probes[0]["packages"] == probes[1]["packages"] and probes[0]["psycopg"] == probes[1]["psycopg"],
            "C control driver/libpq/package contracts differ")
    offline = read_json(root, prefix + "offline-inputs.json")
    require({item["name"] for item in offline} == PURE_TOOL_WHEELS | {"libpq-dev_17.11-0+deb13u1_arm64.deb"},
            "Unexpected offline build artifacts")
    for item in offline:
        require(sha(checked_read(root / prefix / item["name"], root)) == item["sha256"], "Checked build input changed")
        if item["name"] in PURE_TOOL_WHEELS:
            with zipfile.ZipFile(io.BytesIO(checked_read(root / prefix / item["name"], root))) as wheel:
                expanded = 0
                for member in wheel.infolist():
                    safe_name(member.filename)
                    require(not stat.S_ISLNK(member.external_attr >> 16), "Symlink inside build-tool wheel")
                    expanded += member.file_size
                    require(expanded <= MAX_FILE_BYTES, "Unbounded build-tool wheel")
    sources = read_json(root, prefix + "sources.json")
    require(sha(checked_read(root / prefix / "psycopg_c-3.3.6.tar.gz", root)) == sources["sdist_sha256"], "Psycopg sdist changed")
    for filename, expected in summary["source_files"].items():
        safe_name(filename)
        require(sha(checked_read(root / prefix / filename, root)) == expected, "Successful build helper changed: " + filename)
    return summary


def sql_diagnostic_status(root, selected):
    results = []
    for member in sorted(selected):
        if member.startswith("diagnostics/") and PurePosixPath(member).suffix == ".json" and "explain" in PurePosixPath(member).name.lower():
            data = read_json(root, member)
            results.append({"file": member, "status": "success" if data.get("ok") is True else "failed",
                            "plans": len(data.get("plans", [])), "error": data.get("error"), "summaries": data.get("summaries")})
    statuses = {item["status"] for item in results}
    status = "absent" if not statuses else next(iter(statuses)) if len(statuses) == 1 else "mixed"
    return {"status": status, "results": results,
            "scope": "Optional server-side diagnostic; all success/failure attempts retained, no benchmark samples added"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    require(not args.run_root.is_symlink(), "Run root must not be a symlink")
    root = args.run_root.resolve(strict=True)
    destination = args.destination.absolute()
    require(not destination.exists() and not destination.is_symlink(), "Refusing to overwrite destination")
    require(not destination.resolve().is_relative_to(root), "Destination must be outside the run root")
    completion = read_json(root, "primary-series-complete.json")
    require(completion.get("success") is True, "All primary series must finish before archival")
    primary_commands = read_ledger(root, "primary-commands.jsonl")
    require(len(primary_commands) == 3 and all(row.get("exit_code", row.get("returncode")) == 0 for row in primary_commands),
            "Missing three successful primary series launch commands")
    primary_outputs = set()
    for row in primary_commands:
        command = row.get("command", row.get("argv", []))
        require(isinstance(command, list) and "--output" in command, "Primary command omitted its recorded output")
        primary_outputs.add(Path(command[command.index("--output") + 1]).name)
    require(primary_outputs == set(SERIES), "Primary launch commands do not cover all three independent series")
    for name in SERIES:
        checked_read(root / f"logs/series-{name}.log", root)
    series = {name: validate_series(root, name, specification) for name, specification in SERIES.items()}
    require(sum(row["samples"] for row in series.values()) == 192
            and sum(row["requests"] for row in series.values()) == 1536, "Incomplete total experiment")
    receipts, build = validation_receipts(root), validate_build(root)
    fixture = read_json(root, "results/fixture.json")
    require((fixture["releases"], fixture["archives"], fixture["files"]) == (1024, 5248, 43392), "Wrong workbench fixture")
    helpers = read_json(root, "host-tools/manifest.json")
    for filename, expected in helpers.items():
        safe_name(filename)
        require(sha(checked_read(root / "host-tools" / filename, root)) == expected, "Frozen host helper changed: " + filename)
    for variant in ("baseline", "optimized"):
        validate_source_archive(checked_read(root / f"frozen/{variant}/source-backend.tar.gz", root),
                                read_json(root, f"frozen/{variant}/source-manifest.json"))
    for name in ("host-platform.json", "setup-commands.json", "correctness-commands.jsonl", "c-validation-commands.jsonl", "test-commands.jsonl",
                 "diagnostics/profile-used.py", "diagnostics/profile-ast-used.py", "diagnostics/profile-commands.jsonl",
                 "logs/prepare.log", "results/logs/fixture.log"):
        checked_read(root / name, root)
    for variant in ("baseline", "optimized"):
        for mode in ("regular", "ft_off"):
            for suffix in ("ast.json", "c.json", "c.prof"):
                checked_read(root / f"diagnostics/{variant}-{mode}-c1-{suffix}", root)
    selected, omitted = select_files(root)
    sql_status = sql_diagnostic_status(root, selected)
    total_bytes = sum(path.stat().st_size for path in selected.values())
    require(total_bytes <= MAX_TOTAL_BYTES, "Selected evidence exceeds bounded archive size")
    signatures = {name: (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ino) for name, path in selected.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # Atomic no-overwrite reservation; partial failures are preserved.
    members, deliveries = [], []

    def deliver(name, data):
        safe_name(name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        deliveries.append({"name": name, "bytes": len(data), "sha256": sha(data)})

    archive_path = destination / "raw-evidence.tar.gz"
    with archive_path.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, path in sorted(selected.items()):
                current = path.stat()
                require(signatures[name] == (current.st_size, current.st_mtime_ns, current.st_ino), "Evidence changed before compression: " + name)
                data = checked_read(path, root)
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(data))
                members.append({"name": name, "bytes": len(data), "sha256": sha(data)})
    for name in ("comparison-python-analysis.json", "comparison-c-analysis.json", "renderer-c-analysis.json",
                 "host-platform.json", "primary-series-complete.json"):
        deliver(name, checked_read(root / name, root))
    deliver("fixture.json", checked_read(root / "results/fixture.json", root))
    deliver("c-driver-summary.json", checked_read(root / "c-driver/attempt-04/summary.json", root))
    for name, value in (("validation-receipts.json", receipts), ("sql-explain-status.json", sql_status), ("archive-members.json", members)):
        deliver(name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
    for name, path in selected.items():
        current = path.stat()
        require(signatures[name] == (current.st_size, current.st_mtime_ns, current.st_ino), "Evidence changed during archival: " + name)
    with archive_path.open("rb") as handle:
        archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    metadata = {"schema_version": 1, "success": True, "created_utc": datetime.now(timezone.utc).isoformat(),
        "run_root_name": root.name, "archive_script_sha256": sha(Path(__file__).read_bytes()),
        "series": series, "samples": 192, "requests": 1536,
        "archive": {"name": archive_path.name, "bytes": archive_path.stat().st_size, "sha256": archive_hash},
        "archive_member_manifest": next(row for row in deliveries if row["name"] == "archive-members.json"),
        "delivery_files": list(deliveries), "omitted": omitted, "omitted_entries": len(omitted),
        "excluded_runtime_cache_directories": sorted(RUNTIME_CACHE_DIRECTORIES),
        "source_snapshots": "Only frozen/{baseline,optimized}/source-manifest.json and source-backend.tar.gz; verified against nested archive contents",
        "excluded_subtrees": ["pilot-projection", "final-optimized", "frozen/baseline/backend", "frozen/optimized/backend", "c-driver root duplicates"],
        "policy": "Explicit bounded evidence trees; no .env, user data, media, databases, caches, symlinks, special files, or native built wheels; no Docker/SQL/deletion"}
    deliver("manifest.json", (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode())
    checksum_data = "".join(f"{row['sha256']}  {row['name']}\n" for row in [{"name": archive_path.name, "sha256": archive_hash}, *deliveries])
    with (destination / "SHA256SUMS").open("x") as handle:
        handle.write(checksum_data)
    print(json.dumps({"destination": str(destination), "members": len(members), "samples": 192, "requests": 1536,
                      "archive_sha256": archive_hash, "metadata_sha256": deliveries[-1]["sha256"]}))


if __name__ == "__main__":
    main()
