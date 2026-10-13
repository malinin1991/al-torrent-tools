"""Durable release-sync summaries and server-validated, bounded retry targets.

No JobRunner imports at module scope: runner imports JobPartialFailure.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.logging_filters import redact_secrets
from app.utils.datetime_fmt import as_utc_iso, utcnow

SYNC_JOB_TYPES = frozenset({"ongoing", "full_sync", "full_meta_sync", "meta_sync", "force_release_sync", "backoffice_sync", "retry_sync_failures"})
RETRY_LIMIT = 100
_RETRY_OPERATIONS = frozenset({"process", "meta", "force_load", "comment", "tags", "rename", "source_policy"})
_ACTIVE = frozenset({"pending", "running", "stopping"})
_CURRENT: ContextVar[SyncOutcome | None] = ContextVar("sync_outcome", default=None)


class JobPartialFailure(RuntimeError):
    """Terminal signal only; summary and failures have already been committed."""


def safe_message(exc: BaseException | str) -> str:
    # Arbitrary upstream bodies can contain secrets outside known URL patterns.
    if isinstance(exc, BaseException):
        return f"Операция не выполнена ({type(exc).__name__})"
    return redact_secrets(str(exc))[:500]


def current_outcome(db: Session, job_id: int) -> SyncOutcome | None:
    outcome = _CURRENT.get()
    return outcome if outcome is not None and outcome.db is db and outcome.job_id == job_id else None


class SyncOutcome:
    def __init__(self, db: Session, job_id: int, params: dict | None = None):
        self.db, self.job_id = db, job_id
        self.params = params or {}
        self.releases: dict[str, str] = {}
        self.torrents: dict[str, str] = {}
        self.operations: dict[str, int] = {}
        self.failures: dict[str, dict] = {}
        self._saved_attempts: dict[str, int] = {}
        self.discovery_complete = True
        self.scan_complete = False
        self.notes: list[dict] = []
        if db.get(Job, job_id) is None:
            raise ValueError(f"Джоб {job_id} не найден")

    def summary(self) -> dict:
        def counts(objects):
            return {"visited": len(objects), **{key: sum(value == key for value in objects.values()) for key in ("processed", "skipped", "waiting", "errors")}}
        return {"version": 1, "scan_complete": self.scan_complete,
                "discovery_complete": self.discovery_complete,
                "releases": counts(self.releases), "torrents": counts(self.torrents),
                "operations": dict(self.operations), "issue_count": len(self.failures),
                "retry_of": self.params.get("retry_of"), "notes": self.notes[-100:]}

    def save(self) -> None:
        from app.db.models import JobSyncFailure
        job = self.db.get(Job, self.job_id)
        if job is None:
            raise ValueError("Родительский джоб удалён во время синхронизации")
        job.result_json = self.summary()
        job.result_updated_at = utcnow()
        for key, data in self.failures.items():
            if self._saved_attempts.get(key) == data["attempts"]:
                continue
            row = self.db.scalar(select(JobSyncFailure).where(JobSyncFailure.job_id == self.job_id, JobSyncFailure.object_key == key))
            if row is None:
                row = JobSyncFailure(job_id=self.job_id, object_key=key, **data)
                self.db.add(row)
            else:
                row.attempts = data["attempts"]
        self.db.commit()
        self._saved_attempts = {key: data["attempts"] for key, data in self.failures.items()}

    def failure(self, *, release_id: int | None = None, torrent_id: int | None = None,
                info_hash: str | None = None, source: str = "unknown", operation: str = "process",
                qb_role: str | None = None, code: str = "operation_failed", message: BaseException | str,
                retryable: bool = True, retry_params: dict | None = None, scope: str | None = None) -> None:
        scope = scope or ("torrent" if torrent_id is not None else "release" if release_id is not None else "discovery")
        source = source if source in {"site", "backoffice"} else "unknown"
        info_hash = str(info_hash).strip().lower() if info_hash else None
        retryable = bool(retryable and release_id is not None and operation in _RETRY_OPERATIONS)
        if torrent_id is not None and not re.fullmatch(r"[0-9a-f]{32,64}", info_hash or ""):
            retryable = False
            message = safe_message(message) + "; точная версия неизвестна — синхронизируйте релиз"
        key = hashlib.sha256(json.dumps([scope, release_id, torrent_id, info_hash, source, operation, qb_role, code], separators=(",", ":")).encode()).hexdigest()
        old = self.failures.get(key)
        # Persist no arbitrary request parameters or bodies.
        allowed = {k: bool(v) for k, v in (retry_params or {}).items() if k in {"force_qb_load", "notification_baseline"}}
        self.failures[key] = dict(scope=scope, release_id=release_id, torrent_id=torrent_id,
            info_hash=info_hash, source=source, operation=operation, qb_role=qb_role,
            code=code, message=safe_message(message), retryable=retryable,
            retry_params=allowed, attempts=(old["attempts"] + 1 if old else 1))
        if scope == "discovery":
            self.discovery_complete = False
        if release_id is not None:
            self.releases[str(release_id)] = "errors"
        if torrent_id is not None:
            self.torrents[f"{release_id}:{torrent_id}:{info_hash or ''}"] = "errors"
        self.save()

    def torrent(self, release_id: int, torrent_id: int, info_hash: str | None, status: str) -> None:
        key = f"{release_id}:{torrent_id}:{info_hash or ''}"
        rank = {"skipped": 0, "processed": 1, "waiting": 2, "errors": 3}
        if rank.get(status, 0) >= rank.get(self.torrents.get(key), -1):
            self.torrents[key] = status

    def release(self, release_id: int, stats: dict | None = None, *, status: str | None = None) -> None:
        stats = stats or {}
        status = status or ("errors" if stats.get("errors") else "waiting" if stats.get("waiting_master") else "processed" if stats.get("new") or stats.get("added") or stats.get("updated") or any(stats.get(k) for k in ("comments", "tags", "renames")) else "skipped")
        if status != "errors" and any(key.startswith(f"{release_id}:") and value == "waiting" for key, value in self.torrents.items()):
            status = "waiting"
        rank = {"skipped": 0, "processed": 1, "waiting": 2, "errors": 3}
        if rank[status] >= rank.get(self.releases.get(str(release_id)), -1):
            self.releases[str(release_id)] = status
        for key in ("added", "updated", "new", "comments", "tags", "renames"):
            self.operations[key] = self.operations.get(key, 0) + int(stats.get(key, 0) or 0)
        # Older/mocked processors may return only a count, without exact failures.
        if stats.get("errors") and not any(f["release_id"] == release_id for f in self.failures.values()):
            self.failure(release_id=release_id, message="Часть торрентов не обработана", code="release_incomplete")
        self.save()

    def stale(self, release_id: int, torrent_id: int | None, reason: str) -> None:
        self.notes.append({"release_id": release_id, "torrent_id": torrent_id, "code": "stale_target", "message": safe_message(reason)})
        if torrent_id is not None:
            self.torrent(release_id, torrent_id, None, "skipped")
        self.release(release_id, status="skipped")


def sync_job(handler):
    """One collector for a job, including nested full_sync → backoffice_sync."""
    @wraps(handler)
    async def wrapped(db, job_id: int, params: dict):
        existing = current_outcome(db, job_id)
        if existing is not None:
            return await handler(db, job_id, params)
        outcome = SyncOutcome(db, job_id, params)
        token = _CURRENT.set(outcome)
        try:
            outcome.save()
            result = await handler(db, job_id, params)
            outcome.scan_complete = outcome.discovery_complete
            outcome.save()
            if outcome.failures:
                raise JobPartialFailure(f"Завершено с ошибками: {len(outcome.failures)}; подробности в результате джоба")
            return result
        except BaseException as exc:
            # Handlers commit completed objects before their next cooperative stop.
            # Roll back a failed unit, then durably preserve already collected facts.
            db.rollback()
            from app.services.job_runner import JobStopRequested
            if isinstance(exc, Exception) and not isinstance(exc, (JobPartialFailure, JobStopRequested)):
                release_id = params.get("release_id") if isinstance(params, dict) else None
                outcome.failure(release_id=release_id if type(release_id) is int else None,
                    operation="process" if type(release_id) is int else "discovery",
                    code="sync_interrupted", message=exc, retryable=type(release_id) is int)
            outcome.save()
            raise
        finally:
            _CURRENT.reset(token)
    return wrapped


def get_failure_page(db: Session, job_id: int, *, after_id: int = 0, limit: int = 50) -> dict:
    from app.db.models import JobSyncFailure
    limit = max(1, min(100, int(limit)))
    rows = list(db.scalars(select(JobSyncFailure).where(JobSyncFailure.job_id == job_id, JobSyncFailure.id > max(0, after_id)).order_by(JobSyncFailure.id).limit(limit + 1)).all())
    more = len(rows) > limit
    rows = rows[:limit]
    total = int(db.scalar(select(func.count()).select_from(JobSyncFailure).where(JobSyncFailure.job_id == job_id)) or 0)
    return {"items": [{"id": r.id, "scope": r.scope, "release_id": r.release_id, "torrent_id": r.torrent_id,
                "info_hash": r.info_hash, "source": r.source, "operation": r.operation, "qb_role": r.qb_role,
                "code": r.code, "message": r.message, "retryable": r.retryable, "attempts": r.attempts,
                "created_at": as_utc_iso(r.created_at)} for r in rows],
            "next_after_id": rows[-1].id if more and rows else None, "has_more": more, "total": total}


def build_retry_params(db: Session, job_id: int, failure_ids: list[int] | None = None) -> dict:
    """Only server-read targets; caller must never accept client-supplied snapshots."""
    from app.db.models import JobSyncFailure
    job = db.get(Job, job_id)
    if job is None:
        raise ValueError("Исходный джоб не найден")
    if job.type not in SYNC_JOB_TYPES or job.status in _ACTIVE:
        raise ValueError("Повтор доступен только для завершённой синхронизации")
    query = select(JobSyncFailure).where(JobSyncFailure.job_id == job_id, JobSyncFailure.retryable.is_(True))
    if failure_ids is not None:
        if not isinstance(failure_ids, list) or not failure_ids or len(failure_ids) > RETRY_LIMIT or any(type(x) is not int or x <= 0 for x in failure_ids):
            raise ValueError(f"Выберите от 1 до {RETRY_LIMIT} ошибок")
        query = query.where(JobSyncFailure.id.in_(set(failure_ids)))
    rows = list(db.scalars(query.order_by(JobSyncFailure.id).limit(RETRY_LIMIT + 1)).all())
    if len(rows) > RETRY_LIMIT:
        raise ValueError(f"За один запуск можно повторить не более {RETRY_LIMIT} ошибок; выберите порцию")
    if not rows or (failure_ids is not None and {r.id for r in rows} != set(failure_ids)):
        raise ValueError("Неизвестные, чужие или недоступные для повтора ошибки")
    targets = []
    for r in rows:
        if r.release_id is None or r.operation not in _RETRY_OPERATIONS or r.source not in {"site", "backoffice", "unknown"} or r.qb_role not in {None, "master", "slave"}:
            raise ValueError("Ошибка не поддерживает безопасный повтор")
        if r.torrent_id is not None and not re.fullmatch(r"[0-9a-f]{32,64}", r.info_hash or ""):
            raise ValueError("Точная версия торрента неизвестна; выполните синхронизацию релиза")
        targets.append({"failure_id": r.id, "release_id": r.release_id, "torrent_id": r.torrent_id,
            "info_hash": r.info_hash, "source": r.source, "operation": r.operation, "qb_role": r.qb_role,
            "force_qb_load": bool((r.retry_params or {}).get("force_qb_load", False)),
            "notification_baseline": bool((r.retry_params or {}).get("notification_baseline", False))})
    return {"retry_of": job_id, "targets": targets}


def validate_retry_request(db: Session, params: dict) -> dict:
    """Public job-creation validator: reject all arbitrary snapshots/flags."""
    if not isinstance(params, dict) or set(params) - {"retry_of", "failure_ids"} or type(params.get("retry_of")) is not int:
        raise ValueError("Укажите retry_of и failure_ids; готовые targets и флаги принимать нельзя")
    return build_retry_params(db, params["retry_of"], params.get("failure_ids"))


class CatalogScan:
    """Confirm a complete paginated catalogue before any absence-based mutation.

    OpenAPI's catalogue response is data + meta.pagination. Legacy raw lists are
    still processed, but cannot prove completeness and never authorize a sweep.
    """
    def __init__(self, outcome: SyncOutcome, *, operation: str = "catalog"):
        self.outcome, self.operation = outcome, operation
        self.seen: set[int] = set()
        self.total: int | None = None
        self.pages: int | None = None
        self.complete = False

    def accept(self, payload: Any, page: int) -> tuple[list[dict], int]:
        from app.jobs.ongoing import _validated_items
        try:
            items = _validated_items(payload)
        except ValueError:
            self._incomplete()
            return [], page
        ids = [item["id"] for item in items]
        fresh = list({item["id"]: item for item in items if item["id"] not in self.seen}.values())
        duplicate = len(ids) != len(set(ids)) or bool(set(ids) & self.seen)
        self.seen.update(ids)
        pagination = payload.get("meta", {}).get("pagination") if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) else None
        if not isinstance(pagination, dict):
            self._incomplete()
            return fresh, page
        total, pages = pagination.get("total"), pagination.get("total_pages")
        valid = type(total) is int and total >= 0 and type(pages) is int and pages >= 0
        if not valid:
            self._incomplete()
            return fresh, page
        if total == 0 and pages in (0, 1):
            pages = 1
        if pages < 1 or page > pages or (page < pages and not items) or duplicate:
            valid = False
        if self.total is not None and (total != self.total or pages != self.pages):
            valid = False
        for key, expected in (("current_page", page), ("count", len(items))):
            if key in pagination and (type(pagination[key]) is not int or pagination[key] != expected):
                valid = False
        per_page = pagination.get("per_page")
        if per_page is not None:
            if type(per_page) is not int or not 1 <= per_page <= 50:
                valid = False
            elif pages != max(1, (total + per_page - 1) // per_page) or len(items) != min(per_page, max(0, total - (page - 1) * per_page)):
                valid = False
        links = pagination.get("links")
        if isinstance(links, dict) and links.get("next") and page >= pages:
            valid = False
        if len(self.seen) > total or (page == pages and len(self.seen) != total):
            valid = False
        if not valid:
            self._incomplete()
            return fresh, page
        self.total, self.pages = total, pages
        self.complete = page == pages
        return fresh, pages

    def _incomplete(self) -> None:
        self.complete = False
        self.outcome.failure(operation=self.operation, code="catalog_incomplete",
            message="Полнота каталога не подтверждена: неполная или несогласованная пагинация; отсутствующие объекты сохранены", retryable=False)
