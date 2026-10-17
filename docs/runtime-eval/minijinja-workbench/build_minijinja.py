"""Build MiniJinja after Cargo normalizes its sdist lock, refusing dependency changes."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile


VERSION = "3.0.1"
SOURCE_SHA256 = "7e1b012a56414134adc46a79a649b529c35a3b955ccb8407a4ce34441c9842ce"
NORMALIZED_LOCK_SHA256 = "876f00d2ee00cb15a8a72361c708e39f79e52215e9c23fffc948d496696b9ea5"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/e5/b5/"
    "75c93ad8c62bf2bacdf7b691c03b93db2dad9f7eac23b8a2a288c046a431/"
    "minijinja-3.0.1.tar.gz"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(*command: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def normalize_sdist_lock(source: Path, output: Path, environment: dict[str, str]) -> dict:
    """Let Cargo repair workspace/feature edges; reject every new or changed package."""
    lock_path = source / "Cargo.lock"
    original = lock_path.read_text()
    packages = tomllib.loads(original)["package"]
    (output / "upstream-Cargo.lock").write_text(original)
    command = ["cargo", "metadata", "--format-version", "1"]
    result = subprocess.run(command, cwd=source, env=environment, capture_output=True, text=True)
    (output / "cargo-metadata.stdout.json").write_text(result.stdout)
    (output / "cargo-metadata.stderr.txt").write_text(result.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    repaired = lock_path.read_text()
    repaired_packages = tomllib.loads(repaired)["package"]

    def key(package):
        return package["name"], package["version"], package.get("source", "")

    original_by_identity = {key(package): package for package in packages}
    introduced = []
    changed_checksums = []
    for package in repaired_packages:
        prior = original_by_identity.get(key(package))
        if prior is None:
            introduced.append(package)
        elif package.get("checksum") != prior.get("checksum"):
            changed_checksums.append({"before": prior, "after": package})
    retained_identities = {key(package) for package in repaired_packages}
    record = {
        "path": "Cargo.lock",
        "reason": "Cargo normalization for packaged workspace and its active dependency feature edges",
        "metadata_command": command,
        "upstream_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "repaired_sha256": hashlib.sha256(repaired.encode()).hexdigest(),
        "packages_before": len(packages), "packages_after": len(repaired_packages),
        "retained_versions_sources_checksums_unchanged": not introduced and not changed_checksums,
        "added_or_upgraded_packages": introduced,
        "changed_checksums": changed_checksums,
        "removed_packages": [
            {"name": p["name"], "version": p["version"]}
            for p in packages if key(p) not in retained_identities
        ],
    }
    (output / "lock-normalization.json").write_text(json.dumps(record, indent=2) + "\n")
    (output / "Cargo.lock.diff").write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), repaired.splitlines(keepends=True),
        fromfile="Cargo.lock.upstream", tofile="Cargo.lock.repaired",
    )))
    shutil.copy2(lock_path, output / "repaired-Cargo.lock")
    if introduced or changed_checksums:
        raise RuntimeError(
            "Cargo normalization introduced a package/version/source/checksum change; "
            "refusing compilation (see lock-normalization.json)"
        )
    if record["repaired_sha256"] != NORMALIZED_LOCK_SHA256:
        raise RuntimeError("Cargo generated a different lock than the recorded metadata-only diagnostic")
    print(json.dumps({"lock_normalization": {
        key: value for key, value in record.items() if key != "removed_packages"
    }}, sort_keys=True), flush=True)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if os.environ.get("PYTHON_GIL") is not None or "gil" in sys._xoptions:
        parser.error("Build with the interpreter default; forced GIL modes are forbidden")
    if sha256(args.source) != SOURCE_SHA256:
        raise SystemExit("MiniJinja source checksum mismatch")
    if sys.version_info[:3] != (3, 15, 0):
        raise SystemExit(f"Expected CPython 3.15.0, got {sys.version}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    wheel_dir = args.output_dir / "wheels"
    wheel_dir.mkdir(exist_ok=True)
    work = Path("/tmp/altt-minijinja-build")
    work.mkdir(exist_ok=True)
    with tarfile.open(args.source) as archive:
        archive.extractall(work, filter="data")
    source = work / f"minijinja-{VERSION}"
    cargo_manifest = source / "minijinja-py/Cargo.toml"
    manifest_text = cargo_manifest.read_text()
    if 'pyo3 = { version = "0.29.3", features = ["abi3-py310"] }' not in manifest_text:
        raise SystemExit("Unexpected upstream PyO3 dependency: review build inputs")
    binding = (source / "minijinja-py/src/lib.rs").read_text()
    if "#[pymodule(gil_used = false)]" not in binding:
        raise SystemExit("Upstream binding does not declare free-threaded support")
    upstream_lock_sha = sha256(source / "Cargo.lock")
    environment = os.environ.copy()
    environment["PYO3_PYTHON"] = sys.executable
    environment["CARGO_BUILD_JOBS"] = str(args.jobs)
    lock_repair = normalize_sdist_lock(source, args.output_dir, environment)
    command = [
        sys.executable, "-m", "maturin", "build", "--release", "--locked",
        "--interpreter", sys.executable, "--out", str(wheel_dir.resolve()),
    ]
    record = {
        "base_image_argument": args.base_image,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "source_version": VERSION,
        "source_patches": [lock_repair],
        "source_rust_python_code_patches": [],
        "source_cargo_lock_sha256": upstream_lock_sha,
        "effective_cargo_lock_sha256": sha256(source / "Cargo.lock"),
        "binding_manifest_sha256": sha256(cargo_manifest),
        "upstream_pyo3_requirement": '0.29.3 with abi3-py310 (unchanged)',
        "gil_used_false_in_source": True,
        "build_command": command,
        "build_environment": {key: environment[key] for key in ("PYO3_PYTHON", "CARGO_BUILD_JOBS")},
        "inherited_build_flags": {
            key: environment.get(key)
            for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_TARGET_DIR", "PYO3_CONFIG_FILE", "PYO3_USE_ABI3_FORWARD_COMPATIBILITY")
        },
        "python_version": sys.version,
        "python_executable": sys.executable,
        "python_config_args": sysconfig.get_config_var("CONFIG_ARGS"),
        "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
        "gil_enabled_before_build": sys._is_gil_enabled(),
        "python_soabi": sysconfig.get_config_var("SOABI"),
        "platform": platform.platform(),
        "rustc": capture("rustc", "--version", "--verbose"),
        "cargo": capture("cargo", "--version"),
        "maturin": importlib.metadata.version("maturin"),
        "apt_build_packages": capture("dpkg-query", "-W", "rustc", "cargo", "gcc", "libc6-dev"),
    }
    (args.output_dir / "build-inputs.json").write_text(json.dumps(record, indent=2) + "\n")
    subprocess.run(command, cwd=source, env=environment, check=True)
    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one built wheel, found {wheels}")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        wheel_metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
        record["wheel_metadata"] = archive.read(wheel_metadata).decode()
        record["native_members"] = [name for name in archive.namelist() if name.endswith(".so")]
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    if free_threaded and ("cp315t" not in wheel.name or "abi3" in wheel.name):
        raise SystemExit(f"Expected a CPython 3.15t specific wheel, got {wheel.name}")
    record["wheel"] = {"filename": wheel.name, "sha256": sha256(wheel), "size": wheel.stat().st_size}
    record["gil_enabled_after_build"] = sys._is_gil_enabled()
    record["pyo3_build_outputs"] = {
        str(path.relative_to(source)): path.read_text(errors="replace")
        for path in (source / "target").glob("release/build/pyo3-*/output")
    }
    shutil.copy2(source / "Cargo.lock", args.output_dir / "source-Cargo.lock")
    (args.output_dir / "build-provenance.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"wheel": record["wheel"], "free_threaded": free_threaded}, sort_keys=True))


if __name__ == "__main__":
    main()
