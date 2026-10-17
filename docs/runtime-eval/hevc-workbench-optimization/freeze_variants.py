#!/usr/bin/env python3
"""Freeze matched backend variants; baseline service comes from Git HEAD."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backend, output = args.backend.resolve(), args.output.resolve()
    if output.exists():
        parser.error("Refusing to replace source evidence")
    service = "app/services/hevc_workbench.py"
    original = subprocess.check_output(["git", "show", "HEAD:backend/" + service], cwd=backend)
    oracle = (backend / "experiments/hevc_workbench_optimization/baseline_service.py").read_bytes()
    if original != oracle:
        parser.error("The preserved oracle does not match the original Git service")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=backend, text=True).strip()
    for variant in ("baseline", "optimized"):
        destination = output / variant / "backend"
        shutil.copytree(backend, destination, ignore=shutil.ignore_patterns(
            ".env*", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
            ".ruff_cache", "data", "*.pyc", "*.log",
        ))
        if variant == "baseline":
            (destination / service).write_bytes(original)
        files = [{"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(destination.rglob("*")) if path.is_file()]
        record = {"variant": variant, "git_head": head, "files": files,
                  "baseline_service_sha256": hashlib.sha256(original).hexdigest(),
                  "description": "Matched backend snapshots; only production workbench service differs; no .env/user data/caches"}
        (destination.parent / "source-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        archive_path = destination.parent / "source-backend.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(destination, arcname="backend")
        print(json.dumps({"variant": variant, "files": len(files), "archive": str(archive_path),
                          "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
