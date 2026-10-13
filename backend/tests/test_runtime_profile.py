"""Safety boundaries for explicit diagnostics, without contacting a real service."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import profile_runtime


@pytest.mark.parametrize(
    "arguments",
    [
        ["attach", "0"],
        ["dump", "-1"],
        ["attach", "7", "--duration", "0"],
        ["attach", "7", "--duration", "301"],
        ["attach", "7", "--rate", "10001"],
        ["attach", "7", "--async-aware", "--mode", "cpu"],
        ["attach", "7", "--async-aware", "--mode", "gil"],
        ["attach", "7", "--async-aware", "--mode", "exception"],
    ],
)
def test_invalid_sampling_never_launches_profiler(monkeypatch, tmp_path, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments must not launch a subprocess")

    monkeypatch.setattr(profile_runtime.subprocess, "run", forbidden)
    with pytest.raises(SystemExit) as error:
        profile_runtime.main(arguments + ["--output", str(tmp_path / "profile.bin")])
    assert error.value.code == 2


def test_pre315_refuses_before_inspecting_process(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(profile_runtime.sys, "version_info", (3, 14, 8))
    monkeypatch.setattr(profile_runtime.os, "kill", lambda *args: pytest.fail("no target access on 3.14"))
    assert profile_runtime.main(["attach", "42", "--output", str(tmp_path / "profile.bin")]) == 1
    assert "CPython 3.15+" in capsys.readouterr().err


@pytest.mark.parametrize("destination_kind", ["file", "symlink", "directory"])
def test_existing_destination_is_never_replaced(monkeypatch, tmp_path, destination_kind):
    destination = tmp_path / "existing"
    if destination_kind == "file":
        destination.write_bytes(b"keep")
    elif destination_kind == "symlink":
        destination.symlink_to(tmp_path / "does-not-exist")
    else:
        destination.mkdir()
    monkeypatch.setattr(profile_runtime.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess"))
    with pytest.raises(SystemExit) as error:
        profile_runtime.main(["dump", "42", "--output", str(destination)])
    assert error.value.code == 2
    if destination_kind == "file":
        assert destination.read_bytes() == b"keep"
    elif destination_kind == "symlink":
        assert destination.is_symlink()
    else:
        assert destination.is_dir()


def _allow_runtime(monkeypatch):
    monkeypatch.setattr(profile_runtime, "require_runtime", lambda: None)
    monkeypatch.setattr(profile_runtime.os, "kill", lambda pid, signal: None)


def test_async_dump_writes_utf8_with_no_shell_or_blocking_options(monkeypatch, tmp_path):
    _allow_runtime(monkeypatch)
    commands = []

    def run(command, *, stdout, check, timeout):
        commands.append(command)
        assert timeout == 30
        stdout.write("Стек: await worker()\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(profile_runtime.subprocess, "run", run)
    output = tmp_path / "stack.txt"
    assert profile_runtime.main(["dump", "42", "--async-aware", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "Стек: await worker()\n"
    assert "--async-aware" in commands[0]
    assert "--all-threads" not in commands[0]
    assert "--blocking" not in commands[0]
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".altt-profile-*"))


def test_failed_capture_leaves_no_misleading_result(monkeypatch, tmp_path):
    _allow_runtime(monkeypatch)

    def run(command, *, check, timeout):
        Path(command[command.index("--output") + 1]).write_bytes(b"incomplete")
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(profile_runtime.subprocess, "run", run)
    output = tmp_path / "profile.bin"
    assert profile_runtime.main(["attach", "42", "--output", str(output)]) == 3
    assert not output.exists()
    assert not list(tmp_path.glob(".altt-profile-*"))


def test_capture_uses_bounded_cpu_all_threads_and_protects_racing_destination(monkeypatch, tmp_path):
    _allow_runtime(monkeypatch)
    output = tmp_path / "profile with spaces.bin"

    def run(command, *, check, timeout):
        assert command[:4] == [profile_runtime.sys.executable, "-m", "profiling.sampling", "attach"]
        assert command[-1] == "42"
        assert command[command.index("--duration") + 1] == "30"
        assert timeout == 60
        assert command[command.index("--sampling-rate") + 1] == "1000"
        assert command[command.index("--mode") + 1] == "cpu"
        assert "--all-threads" in command
        assert "--binary" in command
        Path(command[command.index("--output") + 1]).write_bytes(b"captured")
        output.write_bytes(b"created by another process")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(profile_runtime.subprocess, "run", run)
    assert profile_runtime.main(["attach", "42", "--mode", "cpu", "--output", str(output)]) == 1
    assert output.read_bytes() == b"created by another process"
    assert not list(tmp_path.glob(".altt-profile-*"))


def test_successful_capture_is_published_without_private_temporary_files(monkeypatch, tmp_path):
    _allow_runtime(monkeypatch)

    def run(command, *, check, timeout):
        Path(command[command.index("--output") + 1]).write_bytes(b"complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(profile_runtime.subprocess, "run", run)
    output = tmp_path / "profile.bin"
    assert profile_runtime.main(["attach", "42", "--output", str(output)]) == 0
    assert output.read_bytes() == b"complete"
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".altt-profile-*"))


def test_interrupted_capture_does_not_publish_partial_output(monkeypatch, tmp_path):
    _allow_runtime(monkeypatch)

    def run(command, **kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"incomplete")
        raise KeyboardInterrupt

    monkeypatch.setattr(profile_runtime.subprocess, "run", run)
    output = tmp_path / "profile.bin"
    assert profile_runtime.main(["attach", "42", "--output", str(output)]) == 130
    assert not output.exists()
    assert not list(tmp_path.glob(".altt-profile-*"))


def test_missing_sampling_package_has_clear_error(monkeypatch):
    monkeypatch.setattr(profile_runtime.sys, "version_info", (3, 15, 0))

    def missing_module(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(profile_runtime.importlib.util, "find_spec", missing_module)
    with pytest.raises(RuntimeError, match="недоступен profiling.sampling"):
        profile_runtime.require_runtime()
