#!/usr/bin/env python3
"""Optional PostgreSQL file-projection diagnostic; run after all benchmark series.

Run inside a matching experiment runtime, with the frozen optimized backend
mounted read-only, e.g. --source-backend /app --output /output/explain.json.
No Docker orchestration or HTTP requests are performed here. The default URL
addresses only the disposable workbench fixture; production databases are refused.

One READ ONLY REPEATABLE READ transaction supplies identical active raw hashes
and one data snapshot. Two warmups precede four paired orders AB, BA, AB, BA.
EXPLAIN's server planning/execution milliseconds are diagnostic: they exclude
ordinary result transfer, DBAPI decoding and Python row construction, include
EXPLAIN instrumentation, and are neither an HTTP benchmark nor server CPU time.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import statistics
import sys
import sysconfig
import tempfile
import threading


DEFAULT_URL = "postgresql+psycopg://postgres@altt-workbench-opt-pg-20261017:5432/altt_minijinja_workbench"
READ_ONLY_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
PROOF_SQL = """SELECT current_database() AS database_name,
current_setting('transaction_read_only') AS transaction_read_only,
current_setting('default_transaction_read_only') AS default_transaction_read_only,
current_setting('transaction_isolation') AS transaction_isolation,
current_setting('server_version') AS server_version,
current_setting('server_version_num') AS server_version_num,
current_setting('plan_cache_mode') AS plan_cache_mode,
current_setting('jit') AS jit, current_setting('track_io_timing') AS track_io_timing,
pg_backend_pid() AS backend_pid"""
EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
ORDERS = (("baseline", "optimized"), ("optimized", "baseline")) * 2


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def source_hashes(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file()
            and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-backend", type=Path, required=True)
    parser.add_argument("--database-url", default=DEFAULT_URL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_backend.resolve()
    if not (source / "app/services/hevc_workbench.py").is_file():
        parser.error("Missing frozen optimized backend")
    if any(source.rglob(".env*")):
        parser.error("Frozen backend must not contain .env files")
    output = args.output.resolve()
    if output.is_relative_to(source):
        parser.error("Write evidence outside the frozen backend")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = output.open("x")
    except FileExistsError:
        parser.error("Refusing to overwrite existing evidence")
    record = {"ok": False, "started_utc": utc(), "source_backend": str(source),
              "driver_sha256": digest(Path(__file__).read_bytes()), "gil_checkpoints": [],
              "plans": [], "sql_audit": [], "commits": 0, "sql_writes": 0,
              "blocked_statement_sha256": [],
              "method": {"warmups_per_variant": 1, "paired_orders": ORDERS,
                         "measured_plans": 8, "total_plans": 10,
                         "scope": "EXPLAIN server wall milliseconds only; no result transfer/DBAPI/Python fetch costs",
                         "limits": "Diagnostic, instrumentation included, warm cache, sequential connection; no CI, CPU or HTTP throughput claims"}}
    engine = connection = transaction = session = None
    failure = None
    expected_gil = not bool(sysconfig.get_config_var("Py_GIL_DISABLED"))

    def check_gil(point):
        enabled = sys._is_gil_enabled()
        record["gil_checkpoints"].append({"point": point, "gil": enabled, "thread_id": threading.get_ident()})
        if enabled != expected_gil:
            raise RuntimeError("Natural effective GIL changed at " + point)

    try:
        if sys.version_info[:2] != (3, 15):
            raise RuntimeError("Use an experimental CPython 3.15 runtime")
        if os.environ.get("PYTHON_GIL") is not None or "gil" in sys._xoptions:
            raise RuntimeError("Natural mode required; all GIL overrides are forbidden")
        check_gil("startup")
        record["source_files_before"] = source_hashes(source)
        record["source_tree_sha256"] = digest(json.dumps(record["source_files_before"], sort_keys=True).encode())
        from sqlalchemy import create_engine, event, select
        from sqlalchemy.engine import make_url
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import NullPool
        url = make_url(args.database_url)
        if url.drivername != "postgresql+psycopg" or not re.fullmatch(r"altt_minijinja[a-zA-Z0-9_]*", url.database or ""):
            raise RuntimeError("Only isolated PostgreSQL psycopg databases named altt_minijinja... are permitted")
        if url.query:
            raise RuntimeError("URL query overrides are forbidden for this isolated diagnostic")
        record["database"] = {"name": url.database, "host": url.host, "port": url.port}
        os.environ.update(APP_ENV="test", DATABASE_URL=args.database_url,
                          SECRET_KEY="isolated-projection-diagnostic",
                          ANILIBRIA_BASE_URL="http://invalid.invalid",
                          ANILIBRIA_FALLBACK_BASE_URL="http://invalid.invalid",
                          ANILIBRIA_ADMIN_BASE_URL="")
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        # Settings resolves .env relative to cwd. No checkout configuration is read.
        with tempfile.TemporaryDirectory(prefix="altt-projection-explain-") as temporary:
            previous_cwd = Path.cwd()
            try:
                os.chdir(temporary)
                from app.services import hevc_workbench as service
                from app.db.models import TorrentArchive, TorrentFile
            finally:
                os.chdir(previous_cwd)
        if Path(service.__file__).resolve() != source / "app/services/hevc_workbench.py":
            raise RuntimeError("Imported service differs from the specified frozen source")
        import psycopg
        check_gil("native_and_application_imports")
        record["runtime"] = {"python": sys.version, "executable": sys.executable,
                             "platform": platform.platform(), "xoptions": dict(sys._xoptions),
                             "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"),
                             "expected_gil": expected_gil, "psycopg_backend": psycopg.pq.__impl__,
                             "libpq_version": psycopg.pq.version(),
                             "packages": dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions()))}
        # The startup option also protects SQLAlchemy's dialect initialization.
        engine = create_engine(url, poolclass=NullPool,
                               connect_args={"options": "-c default_transaction_read_only=on"})
        connection = engine.connect()
        transaction = connection.begin()
        allowed = {READ_ONLY_SQL: "read_only_setup", PROOF_SQL: "read_only_proof"}

        @event.listens_for(connection, "before_cursor_execute")
        def guard(_connection, _cursor, statement, _parameters, _context, _many):
            if statement not in allowed:
                record["blocked_statement_sha256"].append(digest(statement.encode()))
                raise RuntimeError("Refusing SQL outside the explicitly known read-only statements")
            record["sql_audit"].append({"kind": allowed[statement], "statement_sha256": digest(statement.encode())})

        @event.listens_for(connection, "commit")
        def no_commit(_connection):
            record["commits"] += 1
            raise RuntimeError("Read-only diagnostic must not commit")

        connection.exec_driver_sql(READ_ONLY_SQL)

        def proof():
            row = dict(connection.exec_driver_sql(PROOF_SQL).mappings().one())
            if (row["database_name"] != url.database or row["transaction_read_only"] != "on"
                    or row["default_transaction_read_only"] != "on"
                    or row["transaction_isolation"] != "repeatable read"):
                raise RuntimeError("Wrong database or read-only snapshot contract")
            return row

        record["read_only_before"] = proof()
        session = Session(bind=connection, join_transaction_mode="rollback_only")
        archives_query = select(
            TorrentArchive.id, TorrentArchive.release_id, TorrentArchive.torrent_id,
            TorrentArchive.info_hash, TorrentArchive.torrent_type, TorrentArchive.torrent_description,
            TorrentArchive.quality_json, TorrentArchive.created_at, TorrentArchive.api_created_at,
            TorrentArchive.api_present, TorrentArchive.superseded, TorrentArchive.ignore_hevc,
            TorrentArchive.anime_name, TorrentArchive.release_alias,
        )
        allowed[str(archives_query.compile(dialect=engine.dialect))] = "archive_snapshot"
        archives = [service._ArchiveRow(*row) for row in session.execute(archives_query)]
        active = {}
        # Exact source algorithm, raw values and latest active AVC selection.
        for archive in sorted(archives, key=lambda item: item.id, reverse=True):
            if not archive.api_present or archive.superseded or archive.info_hash in active:
                continue
            if service.classify_archive_codec(quality_json=archive.quality_json, torrent_type=archive.torrent_type) == "AVC":
                active[archive.info_hash] = archive
        hashes = list(active)
        if not hashes:
            raise RuntimeError("The fixture has no active AVC hashes; refusing an unrepresentative empty-array diagnostic")
        record["inputs"] = {"archive_rows": len(archives), "active_hash_count": len(hashes),
                            "ordered_active_hashes_sha256": digest(json.dumps(hashes, ensure_ascii=False).encode()),
                            "active_algorithm": "frozen service._ArchiveRow/classify_archive_codec; same descending-id active AVC loop; no normalization"}
        queries = {
            "baseline": select(TorrentFile.id, TorrentFile.release_id, TorrentFile.info_hash, TorrentFile.relative_path,
                               TorrentFile.full_path, TorrentFile.size, TorrentFile.selected, TorrentFile.ui_status,
                               TorrentFile.is_checking, TorrentFile.media_present, TorrentFile.updated_at),
            "optimized": select(*service._file_projection(session, hashes)),
        }
        record["statements"], compiled_queries = {}, {}
        for variant, query in queries.items():
            if not query.is_select:
                raise RuntimeError("Only the known file SELECT projections may be explained")
            compiled = query.compile(dialect=engine.dialect, compile_kwargs={"render_postcompile": True})
            sql, parameters = str(compiled), compiled.params
            expected_columns = 11 if variant == "baseline" else 10
            if len(query.selected_columns) != expected_columns:
                raise RuntimeError("Unexpected frozen projection column count")
            explain_sql = EXPLAIN_PREFIX + sql
            allowed[explain_sql] = "explain_" + variant
            compiled_queries[variant] = (explain_sql, parameters)
            record["statements"][variant] = {
                "select_sql": sql, "select_statement_sha256": digest(sql.encode()),
                "explain_statement_sha256": digest(explain_sql.encode()),
                "columns": list(query.selected_columns.keys()),
                "parameters_sha256": digest(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()),
                "bind_parameter_names": sorted(parameters), "literal_binds": False,
            }
        record["runtime"]["prepare_threshold"] = connection.connection.driver_connection.prepare_threshold
        check_gil("snapshot_and_compilation")

        def explain(variant, phase, round_index, position):
            sql, parameters = compiled_queries[variant]
            plan = connection.exec_driver_sql(sql, parameters).scalar_one()
            if not isinstance(plan, list) or len(plan) != 1:
                raise RuntimeError("Unexpected PostgreSQL JSON plan shape")
            item = plan[0]
            record["plans"].append({"variant": variant, "phase": phase, "round": round_index,
                                    "position": position, "planning_ms": item["Planning Time"],
                                    "execution_ms": item["Execution Time"],
                                    "actual_rows": item["Plan"]["Actual Rows"],
                                    "actual_loops": item["Plan"]["Actual Loops"], "plan": plan})
            check_gil(f"{phase}_{round_index}_{position}_{variant}")

        for position, variant in enumerate(queries):
            explain(variant, "warmup", None, position)
        for round_index, order in enumerate(ORDERS):
            for position, variant in enumerate(order):
                explain(variant, "measurement", round_index, position)
        record["read_only_after"] = proof()
        if len({(item["actual_rows"], item["actual_loops"]) for item in record["plans"]}) != 1:
            raise RuntimeError("The projections did not visit identical row counts")
        record["summaries"] = {}
        for variant in queries:
            plans = [item for item in record["plans"] if item["variant"] == variant and item["phase"] == "measurement"]
            record["summaries"][variant] = {"measured_plans": len(plans), "actual_rows": plans[0]["actual_rows"],
                **{f"{metric}_median_ms": statistics.median(item[f"{metric}_ms"] for item in plans)
                   for metric in ("execution", "planning")},
                "execution_all_ms": [item["execution_ms"] for item in plans],
                "planning_all_ms": [item["planning_ms"] for item in plans]}
        if source_hashes(source) != record["source_files_before"]:
            raise RuntimeError("Frozen backend changed during diagnostics")
        record["source_hashes_verified_after"] = True
        record["read_only_known_statements_only"] = True
        check_gil("end")
        record["ok"] = True
    except BaseException as error:
        failure = error
        record["error"] = {"type": type(error).__name__,
                           "message": str(error) if isinstance(error, RuntimeError) else "Diagnostic failed; inspect runtime/SQL audit and external command log"}
    finally:
        cleanup = (("session_close", session.close if session is not None else None),
                   ("transaction_rollback", transaction.rollback if transaction is not None and transaction.is_active else None),
                   ("connection_close", connection.close if connection is not None else None),
                   ("engine_dispose", engine.dispose if engine is not None else None))
        for name, action in cleanup:
            if action is None:
                continue
            try:
                action()
            except BaseException as error:
                record.setdefault("cleanup_errors", []).append({"point": name, "type": type(error).__name__})
                record["ok"] = False
                failure = failure or error
        record["completed_utc"] = utc()
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.close()
    print(json.dumps({"ok": record["ok"], "output": str(output), "plans": len(record["plans"])}))
    if failure is not None:
        raise SystemExit("Projection diagnostic failed; partial evidence retained")


if __name__ == "__main__":
    main()
