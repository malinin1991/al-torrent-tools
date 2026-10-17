#!/usr/bin/env python3
"""Archive the isolated experiment after all timed runs have finished.

Example (do not run while measuring):
    python3 archive_evidence.py --root /private/tmp/altt-minijinja-eval-20261017 \
        --output /absolute/repository/docs/runtime-eval/minijinja-workbench/evidence

Only explicit evidence directories are selected. No Docker commands, application
imports, source-tree traversal, media mounts, or network access are performed.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile


DENIED_PARTS = {".aws", ".ssh", ".git", ".docker", ".codex", ".agents", "__pycache__", "media", "torrents", "data"}
DENIED_NAMES = {".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "auth.json", "id_rsa", "id_ed25519"}
MEDIA_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".mp3", ".flac", ".wav", ".torrent", ".db", ".sqlite", ".sqlite3"}
BINARY_SUFFIXES = {".whl", ".so", ".dylib", ".dll", ".pyc", ".pyo"}
CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*[\"']?(?:bearer|basic)\s+[A-Za-z0-9+/_=.:-]{12,}"),
    re.compile(rb"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    if any(part.lower() in DENIED_PARTS or part.lower().startswith(".env") for part in path.parts):
        raise ValueError(f"Private/data path cannot be archived: {name}")
    if path.name.lower() in DENIED_NAMES or path.suffix.lower() in MEDIA_SUFFIXES:
        raise ValueError(f"Credential/media/database file cannot be archived: {name}")
    return path


def checked_read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Evidence must be a regular file: {path.name}")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"Evidence changed during archival: {path.name}")
    return data


def check_plain_content(data: bytes, name: str) -> None:
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(data):
            raise ValueError(f"Potential credential content in {name}; curate it before archival")


def check_source_archive(data: bytes) -> None:
    """Validate the existing frozen archive without extracting or rewriting it."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            safe_name(member.name)
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError(f"Non-regular frozen source member: {member.name}")
            if PurePosixPath(member.name).suffix.lower() in BINARY_SUFFIXES:
                raise ValueError(f"Compiled/cache artifact inside frozen source: {member.name}")


def source_metadata(context: Path) -> dict:
    """Read constants as syntax, never execute the used build helper."""
    helper = checked_read(context / "build_minijinja.py")
    constants = {}
    for statement in ast.parse(helper.decode()).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in {"VERSION", "SOURCE_SHA256", "SOURCE_URL"}:
                    constants[target.id] = ast.literal_eval(statement.value)
    if set(constants) != {"VERSION", "SOURCE_SHA256", "SOURCE_URL"}:
        raise ValueError("Used build helper does not contain explicit source provenance")
    artifact = context / f"minijinja-{constants['VERSION']}.tar.gz"
    verified = False
    if artifact.exists():
        actual = digest(checked_read(artifact))
        if actual != constants["SOURCE_SHA256"]:
            raise ValueError("Used source archive checksum differs from the used build helper")
        verified = True
    return {
        "package": "minijinja", "version": constants["VERSION"],
        "url": constants["SOURCE_URL"], "sha256": constants["SOURCE_SHA256"],
        "original_source_archive_present_and_verified": verified,
        "used_build_helper_sha256": digest(helper),
        "note": "Public source sdist omitted from this evidence archive; recover it by URL and SHA-256.",
    }


def selected_files(root: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    selected = []
    omitted = []
    directories = ["results", "logs", "build-evidence", "build-context"]
    if (root / "results-uninterrupted").exists():
        directories.append("results-uninterrupted")
    if (root / "host-inputs").exists():
        directories.append("host-inputs")
    for directory in directories:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f"Missing regular evidence directory: {directory}")
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f"Symlinks are not evidence: {relative}")
            if path.is_dir():
                continue
            if path.name == "docker-info.json" or path.suffix.lower() in BINARY_SUFFIXES:
                omitted.append(relative)
                continue
            if directory == "build-context" and re.fullmatch(r"minijinja-[0-9.]+\.tar\.gz", path.name):
                omitted.append(relative)
                continue
            safe_name(relative)
            selected.append((path, relative))
    for name in ("source-manifest.json", "source-backend.tar.gz"):
        path = root / "frozen" / name
        safe_name(f"frozen/{name}")
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing frozen source evidence: {name}")
        selected.append((path, f"frozen/{name}"))
    for name in ("analysis.json", "suspension-audit.json"):
        path = root / name
        if path.exists():
            safe_name(name)
            selected.append((path, name))
    if not (root / "results/environment-summary.json").is_file():
        raise ValueError("environment-summary.json is required instead of full docker-info.json")
    return selected, omitted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--curated-metadata", action="store_true", help="Also copy validation/provenance JSON alongside the raw archive")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("Refusing to overwrite an existing output path")
    if output.resolve().is_relative_to(root):
        parser.error("Output must be outside the temporary experiment root")
    files, omitted = selected_files(root)
    provenance = source_metadata(root / "build-context")
    metadata_bytes = (json.dumps(provenance, indent=2) + "\n").encode()
    check_plain_content(metadata_bytes, "source-artifact.json")
    analysis_source = next((path for path in (
        root / "analysis.json", root / "results-uninterrupted/analysis.json", root / "results/analysis.json"
    ) if path.is_file()), None)
    environment_source = next((path for path in (
        root / "results-uninterrupted/environment-summary.json", root / "results/environment-summary.json"
    ) if path.is_file()), None)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        members = []
        deliveries = []
        archive_path = staging / "raw-evidence.tar.gz"
        with tarfile.open(archive_path, mode="w:gz", compresslevel=6) as archive:
            def add(name: str, data: bytes) -> None:
                safe_name(name)
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(data))
                members.append({"name": name, "size": len(data), "sha256": digest(data)})

            for path, name in files:
                data = checked_read(path)
                if name == "frozen/source-backend.tar.gz":
                    check_source_archive(data)
                else:
                    check_plain_content(data, name)
                add(name, data)
                compact_name = None
                if path == analysis_source:
                    compact_name = "analysis.json"
                elif path == environment_source:
                    compact_name = "environment-summary.json"
                elif args.curated_metadata and name.startswith(("results/", "results-uninterrupted/")) and path.suffix == ".json" and re.match(r"(?:provenance|tests|correctness)-", path.name):
                    compact_name = f"metadata/{PurePosixPath(name).parts[0]}/{path.name}"
                elif args.curated_metadata and name.startswith("results/") and path.name in {"fixture.json", "suspension-audit.json"}:
                    compact_name = f"metadata/results/{path.name}"
                elif args.curated_metadata and name.startswith("build-evidence/") and path.name in {"build-provenance.json", "runtime-probe.json", "lock-normalization.json"}:
                    compact_name = f"metadata/builds/{PurePosixPath(name).parts[1]}/{path.name}"
                elif args.curated_metadata and name == "results-uninterrupted/preview-smoke/summary.json":
                    compact_name = "metadata/preview-smoke/summary.json"
                if compact_name:
                    target = staging / compact_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    compact = (json.dumps(json.loads(data), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                    target.write_bytes(compact)
                    deliveries.append({"name": compact_name, "size": len(compact), "sha256": digest(compact)})
            add("source-artifact.json", metadata_bytes)

        archive_hash = digest(archive_path.read_bytes())
        manifest = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_root_name": root.name,
            "archive_script_sha256": digest(Path(__file__).read_bytes()),
            "used_build_helper_sha256": provenance["used_build_helper_sha256"],
            "compact_analysis_source": analysis_source.relative_to(root).as_posix() if analysis_source else None,
            "interrupted_and_uninterrupted_series_preserved_separately": (root / "results-uninterrupted").is_dir(),
            "archive": {"name": archive_path.name, "size": archive_path.stat().st_size, "sha256": archive_hash},
            "omitted": omitted,
            "archive_members": members,
            "delivery_files": deliveries,
            "policy": "Explicit evidence directories; no full Docker info, source sdist, wheels, .env, credentials, media, database files, symlinks, or unsafe tar paths.",
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        checksums = [(archive_path.name, archive_hash), (manifest_path.name, digest(manifest_path.read_bytes()))]
        checksums.extend((item["name"], item["sha256"]) for item in deliveries)
        (staging / "SHA256SUMS").write_text("".join(f"{sha}  {name}\n" for name, sha in checksums))
        if output.exists() or output.is_symlink():
            raise ValueError("Output appeared during archival; refusing to overwrite it")
        staging.rename(output)
        print(json.dumps({"output": str(output), "members": len(members), "archive_sha256": archive_hash}, sort_keys=True))
    except BaseException:
        shutil.rmtree(staging)
        raise


if __name__ == "__main__":
    main()
