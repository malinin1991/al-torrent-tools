"""Repair a missing _decimal in the pinned regular CPython Docker image.

Build only CPython's extension target, using its own Makefile and bundled
libmpdec. Keep VERSION/SOURCE_SHA256 in sync with the Docker FROM version.
"""

from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request


VERSION = "3.15.0"
SOURCE_SHA256 = "ba4bed1ba346b916890b76d9e320451420aa69f6408997d33c66482eeae3d575"
SOURCE_URL = f"https://www.python.org/ftp/python/{VERSION}/Python-{VERSION}.tar.xz"


def main() -> None:
    if (
        sys.implementation.name != "cpython"
        or sys.platform != "linux"
        or tuple(sys.version_info[:3]) != tuple(map(int, VERSION.split(".")))
        or sys.version_info.releaselevel != "final"
        or sysconfig.get_config_var("Py_GIL_DISABLED")
        or sysconfig.get_config_var("Py_DEBUG")
        or sysconfig.get_config_var("Py_TRACE_REFS")
    ):
        raise RuntimeError(f"Expected regular release CPython {VERSION} on Linux")

    try:
        native = importlib.import_module("_decimal")
    except ModuleNotFoundError as exc:
        if exc.name != "_decimal":
            raise
    else:
        print(f"Existing native _decimal: libmpdec {native.__libmpdec_version__}", flush=True)
        return

    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    destination = Path(sysconfig.get_config_var("DESTSHARED"))
    if not suffix or not destination.is_dir():
        raise RuntimeError("Missing installed CPython extension ABI/path metadata")

    with tempfile.TemporaryDirectory(prefix="altt-decimal-") as temporary:
        build = Path(temporary)
        archive = build / "python.tar.xz"
        digest = hashlib.sha256()
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != SOURCE_SHA256:
            raise RuntimeError("Official CPython source SHA-256 mismatch")

        with tarfile.open(archive) as source_archive:
            source_archive.extractall(build, filter="data")
        source = build / f"Python-{VERSION}"
        subprocess.run(
            ["./configure", "--enable-shared", "--without-system-libmpdec", "--with-ensurepip=no"],
            cwd=source, check=True,
        )
        target = f"Modules/_decimal{suffix}"
        subprocess.run(["make", "-j2", target], cwd=source, check=True)
        binary = source / target
        subprocess.run(["strip", "--strip-unneeded", str(binary)], check=True)
        shutil.copy2(binary, destination / binary.name)

        # Exercise the new extension against the exact upstream decimal suite.
        environment = dict(os.environ, PYTHONPATH=str(source / "Lib"), PYTHONDONTWRITEBYTECODE="1")
        subprocess.run(
            [sys.executable, "-m", "unittest", "test.test_decimal"],
            env=environment, check=True,
        )


if __name__ == "__main__":
    main()
