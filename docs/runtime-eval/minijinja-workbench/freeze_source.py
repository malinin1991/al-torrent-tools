#!/usr/bin/env python3
"""Create a secret-free, checksummed backend snapshot for this experiment."""
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
    backend = args.backend.resolve()
    destination = args.output.resolve()
    if destination.exists():
        raise SystemExit("Refusing to replace an existing source snapshot")
    if any((destination.parent / name).exists() for name in ("source-manifest.json", "source-backend.tar.gz")):
        raise SystemExit("Refusing to replace existing source evidence; choose a fresh parent directory")
    if not (backend / "app/api/hevc_workbench.py").is_file():
        raise SystemExit("Expected ALTT backend source directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(backend, destination, ignore=shutil.ignore_patterns(
        ".env*", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", "data", "*.pyc", "*.log",
    ))
    entries = [{"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in sorted(destination.rglob("*")) if path.is_file()]
    record = {
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=backend, text=True).strip(),
        "description": "Frozen backend including opt-in experiment; .env/user data/venv/caches excluded",
        "files": entries,
    }
    manifest = destination.parent / "source-manifest.json"
    manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    archive_path = destination.parent / "source-backend.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(destination, arcname="backend")
    print(json.dumps({"files": len(entries), "manifest": str(manifest), "archive": str(archive_path),
                      "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
