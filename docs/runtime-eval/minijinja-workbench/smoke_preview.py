#!/usr/bin/env python3
"""Functional Uvicorn/TCP check, separate from all performance measurements.

Prepared for use only AFTER timed series have stopped. Starts only its own
read-only FT containers, one engine at a time, with localhost:18080 published.
Checks C1/C4 requests, semantic HTML, natural GIL-off and real render-worker IDs.
The preview has no business write/API routes or SSE. Saves all evidence and
stops/removes only the exact container it created, in finally.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import traceback
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


class SemanticHTML(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.events, self.stack = [], []
        self.feed(value)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.events.append(["start", tag, sorted(attrs)])
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.events.append(["end", tag])
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, value):
        if not any(tag in {"pre", "code", "script", "style", "textarea"} for tag in self.stack):
            value = re.sub(r"\s+", " ", value).strip()
        if value:
            self.events.append(["text", value])


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fetch(base, path):
    with urlopen(base + path, timeout=180) as response:
        body = response.read().decode("utf-8")
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP {response.status}: {path}")
        return body


def validate_runtime(record, expected_calls):
    assert record["py_gil_disabled"] == 1, record
    assert record["gil_enabled"] is False and record["expected_gil"] is False
    assert record["forced_gil_off"] is False and record["xoptions"].get("gil") != "0"
    assert all(point["gil"] is False for point in record["gil_checkpoints"])
    assert record["template_calls"] == record["completed_calls"] == expected_calls
    assert record["failed_calls"] == record["active_calls"] == 0
    workers = record["template_worker_thread_ids"]
    if expected_calls:
        assert workers and all(worker != record["main_thread_id"] for worker in workers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview-script", type=Path, default=Path(__file__).with_name("preview.py"))
    parser.add_argument("--image", default="altt-minijinja-eval:ft-20261017")
    parser.add_argument("--network", default="altt-minijinja-20261017")
    parser.add_argument("--media-volume", default="altt-minijinja-media-20261017")
    parser.add_argument("--database-url", default="postgresql+psycopg://postgres@altt-minijinja-pg-20261017:5432/altt_minijinja_workbench")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    source, preview = args.source.resolve(), args.preview_script.resolve()
    if not (source / "experiments/minijinja_workbench/prototype.py").is_file() or not preview.is_file():
        parser.error("Provide the frozen experiment backend and preview.py")
    if any(source.glob(".env*")):
        parser.error("Frozen source must not contain .env files")
    database = urlsplit(args.database_url)
    if not database.scheme.startswith("postgresql") or not re.fullmatch(r"altt_minijinja[a-zA-Z0-9_]*", database.path.lstrip("/")):
        parser.error("Only the experiment PostgreSQL database is permitted")
    output = args.output.resolve() / "preview-smoke"
    if output.exists():
        parser.error("Refusing to overwrite preview-smoke; choose a new output directory")
    output.mkdir(parents=True)
    base = f"http://127.0.0.1:{args.port}"
    paths = [path + "?" + urlencode(params)
             for path in ("/hevc-workbench", "/hevc-workbench/live")
             for params in ({"view": "all", "page": 1, "per_page": 30},
                            {"view": "ready", "search": "Опасное", "page": 1, "per_page": 30})]
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True).strip()
    record = {
        "purpose": "Functional Uvicorn/TCP correctness and worker-GIL check; no performance measurements",
        "image": args.image, "image_id": image_id, "source": str(source), "engines": {},
        "preview_script_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
        "utc": datetime.now(timezone.utc).isoformat(),
    }
    baseline = {}
    bridge_id = None
    try:
        # Docker 29 does not publish HostPorts on an internal-only network.
        # The server joins a disposable bridge for localhost publication and
        # the isolated DB network for PostgreSQL. No production networks join.
        bridge_command = ["docker", "network", "create", "--label", "altt.minijinja-preview-smoke=20261017",
                          f"altt-minijinja-preview-{int(time.time())}-{os.getpid()}"]
        bridge_id = subprocess.check_output(bridge_command, text=True).strip()
        record["preview_bridge"] = {"id": bridge_id, "create_command": bridge_command}
        for engine in ("current_jinja", "minijinja"):
            container_id = None
            name = f"altt-minijinja-preview-smoke-{engine}-{int(time.time())}"
            command = [
                "docker", "create", "--name", name, "--read-only", "--network", bridge_id,
                "--label", "altt.minijinja-preview-smoke=20261017", "--tmpfs", "/tmp:rw,size=268435456",
                "--publish", f"127.0.0.1:{args.port}:8000",
                "--mount", f"type=bind,source={source},target=/app,readonly",
                "--mount", f"type=bind,source={preview},target=/preview.py,readonly",
                "--mount", f"type=volume,source={args.media_volume},target=/bench/media,readonly",
                "--workdir", "/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/app",
                args.image, "/usr/local/bin/python", "/preview.py", "--backend", "/app",
                "--engine", engine, "--database-url", args.database_url,
                "--media-root", "/bench/media", "--host", "0.0.0.0", "--port", "8000", "--expect-gil", "0",
            ]
            commands = [command]
            engine_record = {"requests": [], "commands": commands}
            engine_semantics = {}
            record["engines"][engine] = engine_record
            try:
                container_id = subprocess.check_output(command, text=True).strip()
                commands.append(["docker", "network", "connect", args.network, container_id])
                subprocess.check_call(commands[-1], stdout=subprocess.DEVNULL)
                commands.append(["docker", "start", container_id])
                subprocess.check_call(commands[-1], stdout=subprocess.DEVNULL)
                deadline = time.monotonic() + 90
                while True:
                    try:
                        initial = json.loads(fetch(base, "/_experiment/runtime"))
                        validate_runtime(initial, 0)
                        break
                    except (URLError, ConnectionError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Preview did not become ready")
                        time.sleep(.25)
                engine_record["runtime_before"] = initial
                for concurrency in (1, 4):
                    barrier = threading.Barrier(4) if concurrency == 4 else None

                    def request(path):
                        if barrier is not None:
                            barrier.wait()
                        body = fetch(base, path)
                        semantic = SemanticHTML(body).events
                        if path in engine_semantics:
                            assert semantic == engine_semantics[path], "Response changed between C1 and C4"
                        else:
                            engine_semantics[path] = semantic
                        key = (concurrency, path)
                        if engine == "current_jinja":
                            baseline[key] = semantic
                        elif semantic != baseline[key]:
                            raise RuntimeError(f"DOM mismatch: C{concurrency} {path}")
                        if "search=" in path:
                            assert "Доступность изменилась при проверке" in body
                        return {"url": path, "concurrency": concurrency, "status": 200,
                                "semantic_sha256": digest(semantic), "html_sha256": hashlib.sha256(body.encode()).hexdigest(),
                                "body_bytes": len(body.encode()), "client_thread_id": threading.get_ident()}

                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        rows = list(pool.map(request, paths))
                    engine_record["requests"].extend(rows)
                    current = json.loads(fetch(base, "/_experiment/runtime"))
                    validate_runtime(current, 4 if concurrency == 1 else 8)
                    if concurrency == 4:
                        assert len({row["client_thread_id"] for row in rows}) == 4
                        assert len(current["template_worker_thread_ids"]) >= 2
                    engine_record[f"runtime_after_c{concurrency}"] = current
                engine_record["success"] = True
            finally:
                if container_id is not None:
                    logs = subprocess.run(["docker", "logs", container_id], capture_output=True, text=True)
                    (output / f"{engine}.log").write_text(logs.stdout + logs.stderr)
                    commands.append(["docker", "stop", "--time", "5", container_id])
                    subprocess.run(commands[-1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    commands.append(["docker", "rm", container_id])
                    removed = subprocess.run(commands[-1], capture_output=True, text=True)
                    engine_record["container_removed"] = removed.returncode == 0
                    if removed.returncode:
                        engine_record["cleanup_error"] = removed.stderr.strip()
                        engine_record["success"] = False
                (output / f"{engine}.json").write_text(json.dumps(engine_record, ensure_ascii=False, indent=2) + "\n")
                if engine_record.get("container_removed") is False:
                    raise RuntimeError("Failed to stop/remove this driver's preview container: " + container_id)
        record["success"] = True
    except Exception as exc:
        record.update(success=False, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if bridge_id is not None:
            removed = subprocess.run(["docker", "network", "rm", bridge_id], capture_output=True, text=True)
            record["preview_bridge"]["removed"] = removed.returncode == 0
            if removed.returncode:
                record.update(success=False, bridge_cleanup_error=removed.stderr.strip())
    (output / "summary.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "success": record["success"]}), flush=True)
    raise SystemExit(0 if record["success"] else 1)


if __name__ == "__main__":
    main()
