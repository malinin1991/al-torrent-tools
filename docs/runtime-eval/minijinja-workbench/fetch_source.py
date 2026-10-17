"""Fetch the pinned public MiniJinja source artifact into a disposable build context."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

from build_minijinja import SOURCE_SHA256, SOURCE_URL, VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifact = args.output_dir / f"minijinja-{VERSION}.tar.gz"
    if artifact.exists():
        data = artifact.read_bytes()
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
            data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"Source checksum mismatch: {digest}")
    artifact.write_bytes(data)
    metadata = {
        "package": "minijinja", "version": VERSION, "filename": artifact.name,
        "source_url": SOURCE_URL, "sha256": digest,
        "package_metadata_url": f"https://pypi.org/pypi/minijinja/{VERSION}/json",
    }
    (args.output_dir / "minijinja-source.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
