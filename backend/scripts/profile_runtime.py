#!/usr/bin/env python3
"""Explicit, bounded Python 3.15 sampling of a local process; no app imports."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("ожидается целое число") from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"допустимый диапазон: {minimum}..{maximum}")
        return number

    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python 3.15: профиль или снимок стека явно выбранного локального PID."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    attach = subparsers.add_parser("attach", help="ограниченный по времени профиль")
    dump = subparsers.add_parser("dump", help="один снимок стека")
    for command in (attach, dump):
        command.add_argument("pid", type=_bounded_int(1, 2**31 - 1))
        command.add_argument("--output", type=Path, required=True, help="новый файл; без перезаписи")
        command.add_argument(
            "--async-aware", action="store_true", help="цепочки await вместо стеков всех потоков"
        )
    attach.add_argument("--duration", type=_bounded_int(1, 300), default=30, help="секунд, 1..300")
    attach.add_argument("--rate", type=_bounded_int(1, 10000), default=1000, help="samples/sec, 1..10000")
    attach.add_argument("--mode", choices=("wall", "cpu", "gil", "exception"), default="wall")
    attach.add_argument("--format", choices=("binary", "flamegraph", "pstats"), default="binary")
    args = parser.parse_args(argv)
    if args.action == "attach" and args.async_aware and args.mode != "wall":
        parser.error("--async-aware совместим только с --mode wall")
    args.output = args.output.expanduser().absolute()
    if os.path.lexists(args.output):
        parser.error(f"файл уже существует: {args.output}")
    if not args.output.parent.is_dir():
        parser.error(f"каталог не существует: {args.output.parent}")
    return args


def require_runtime() -> None:
    if sys.implementation.name != "cpython" or sys.version_info < (3, 15):
        raise RuntimeError("нужен CPython 3.15+ с profiling.sampling; используйте его python для запуска")
    try:
        available = importlib.util.find_spec("profiling.sampling") is not None
    except ModuleNotFoundError:
        available = False
    if not available:
        raise RuntimeError("в этом CPython недоступен profiling.sampling")


def profiler_command(args: argparse.Namespace, temporary_output: Path) -> list[str]:
    command = [sys.executable, "-m", "profiling.sampling", args.action]
    if args.async_aware:
        command += ["--async-aware", "--async-mode", "all" if args.action == "dump" else "running"]
    else:
        command.append("--all-threads")
    if args.action == "attach":
        command += [
            "--duration", str(args.duration),
            "--sampling-rate", str(args.rate),
            "--mode", args.mode,
            f"--{args.format}",
            "--output", str(temporary_output),
        ]
    command.append(str(args.pid))
    return command


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_runtime()
        # Signal 0 checks existence/permission only; PID 0 and process groups are rejected above.
        os.kill(args.pid, 0)
        with tempfile.TemporaryDirectory(prefix=".altt-profile-", dir=args.output.parent) as directory:
            temporary_output = Path(directory) / args.output.name
            command = profiler_command(args, temporary_output)
            print(shlex.join(command), file=sys.stderr)
            if args.action == "dump":
                with temporary_output.open("w", encoding="utf-8") as output:
                    result = subprocess.run(command, stdout=output, check=False, timeout=30)
            else:
                result = subprocess.run(command, check=False, timeout=args.duration + 30)
            if result.returncode:
                print(
                    "Профилировщик завершился с ошибкой. Проверьте PID и права чтения памяти процесса; "
                    "автоматическое повышение прав не выполняется.",
                    file=sys.stderr,
                )
                return result.returncode if result.returncode > 0 else 1
            # Both paths are on the same filesystem. link() publishes without overwriting,
            # including when another process creates the destination during profiling.
            os.chmod(temporary_output, 0o600)
            os.link(temporary_output, args.output)
        print(f"Сохранено: {args.output}")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Профиль не сохранён: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Профилирование прервано; итоговый файл не сохранён.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
