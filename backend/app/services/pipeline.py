from datetime import timedelta
from app.utils.datetime_fmt import utcnow
from typing import Any, Literal

import qbittorrentapi
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import ExtraUrl, JobLog, PipelineEvent, QbClient, TorrentArchive, TorrentPipeline
from app.services.qbittorrent import (
    MASTER_UI_LABELS,
    SLAVE_UI_LABELS,
    ensure_announce_passkey,
    is_qb_wait_error_text,
    map_qb_torrent_ui_state,
    qb_add_torrent,
    qb_client_wait_message,
    should_wait_for_qb,
)
from app.services.runtime_settings import get_setting_value
from app.services.torrent_archive import TorrentArchiveService
from app.services.torrent_qb_meta import (
    build_qb_torrent_name_from_archive,
    first_release_torrents_url,
    genres_from_quality_json,
    resolve_anilibria_site_url,
)

MasterTorrentState = Literal["complete", "in_progress", "missing"]
SlaveTorrentState = Literal["complete", "in_progress", "missing"]
CiStageState = Literal["pending", "running", "success", "failed", "cancelled", "skipped"]

# created | status_change | master_add | slave_add | hash_enqueued | hash_progress |
# hash_done | hash_fail | tg_queued | tg_sent | cancelled | failed | ui_status |
# hevc_status
PipelineActor = Literal["job", "webhook", "poll", "manual"]

# Горизонтальный путь (master напрямую связан со slave).
_CI_MAIN_DEFS: tuple[tuple[str, str], ...] = (
    ("discover", "discover"),
    ("master", "master"),
    ("slave", "slave"),
    ("done", "done"),
)

_CI_RUNNING_STAGE: dict[str, int | None] = {
    "discovered": 0,
    "waiting_master": 0,
    "master_added": 1,
    "master_complete": 2,
    "waiting_slave": 2,
    "slave_added": 2,
    "done": None,
}

_CI_SUCCESS_BEFORE: dict[str, int] = {
    "discovered": 0,
    "waiting_master": 0,
    "master_added": 1,
    "master_complete": 2,
    "waiting_slave": 2,
    "slave_added": 2,
    "done": 4,
}

_SLAVE_STAGE_INDEX = 2
_HASH_EVENT_TYPES = frozenset({"hash_enqueued", "hash_progress", "hash_done", "hash_fail"})
_CHECK_EVENT_TYPES = _HASH_EVENT_TYPES | frozenset({"ui_status"})


def _ci_tg_state(tg_status: str | None, *, tracked: bool) -> CiStageState:
    if not tracked:
        return "skipped"
    raw = (tg_status or "skipped").strip().lower()
    if raw == "sent":
        return "success"
    if raw in {"pending", "queued"}:
        return "running"
    if raw == "skipped":
        return "skipped"
    return "pending"


def _ci_check_state(files_status: str | None) -> CiStageState:
    """Стадия check (sync_composition / hash)."""
    raw = (files_status or "pending").strip().lower()
    if raw in {"success", "sent", "synced"}:
        return "success"
    if raw in {"running", "queued", "pending_job"}:
        return "running"
    if raw in {"failed", "fail"}:
        return "failed"
    return "pending"


def _ci_fail_stage_index(
    *,
    master_added_at: Any,
    slave_added_at: Any,
    error: str | None,
) -> int:
    """Индекс main-стадии с ошибкой: discover(0) / master(1) / slave(2)."""
    err = (error or "").lower()
    if slave_added_at is not None:
        return _SLAVE_STAGE_INDEX
    if master_added_at is not None:
        if any(token in err for token in ("slave", "waiting_slave", "на slave")):
            return _SLAVE_STAGE_INDEX
        return 1
    return 0


def _side_tg_state(
    *,
    pipeline_status: str,
    tg_status: str | None,
    tracked: bool,
) -> CiStageState:
    base = _ci_tg_state(tg_status, tracked=tracked)
    if base == "skipped":
        return "skipped"
    raw = pipeline_status
    if raw == "discovered":
        return "pending"
    if raw == "waiting_master":
        return base if base != "pending" else "pending"
    return base


def _side_delta_tg_state(
    *,
    tracked: bool,
    files_status: str | None,
    check_state: CiStageState,
) -> CiStageState:
    """Δtg только после hash_done (success), не после раннего sync_composition."""
    if not tracked:
        return "skipped"
    raw = (files_status or "pending").strip().lower()
    if raw == "success":
        return "success"
    if raw == "failed" or check_state == "failed":
        return "failed"
    return "pending"


def pipeline_ci_stages(
    status: str,
    *,
    master_added_at: Any = None,
    slave_added_at: Any = None,
    tg_status: str | None = None,
    files_status: str | None = None,
    tracked: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """CI graph-дерево.

    main:        discover → master → slave → done
    tg_fork:     от ребра discover——master ↓ tg
    check_fork:  от ребра master——slave ↘ check → Δtg
    """
    raw = (status or "").strip().lower()
    n = len(_CI_MAIN_DEFS)
    main_states: list[CiStageState] = ["pending"] * n

    if raw == "done":
        main_states = ["success"] * n
    elif raw in {"failed", "cancelled"}:
        terminal: CiStageState = "failed" if raw == "failed" else "cancelled"
        fail_idx = _ci_fail_stage_index(
            master_added_at=master_added_at,
            slave_added_at=slave_added_at,
            error=error,
        )
        for i in range(fail_idx):
            main_states[i] = "success"
        main_states[fail_idx] = terminal
    elif raw in _CI_RUNNING_STAGE:
        running_idx = _CI_RUNNING_STAGE[raw]
        success_before = _CI_SUCCESS_BEFORE.get(raw, 0)
        for i in range(success_before):
            main_states[i] = "success"
        if running_idx is not None:
            main_states[running_idx] = "running"

    main = [
        {"id": stage_id, "label": label, "state": main_states[i]}
        for i, (stage_id, label) in enumerate(_CI_MAIN_DEFS)
    ]

    check_state = _ci_check_state(files_status)
    # tg: после discover / на пути к master (в т.ч. waiting_master)
    show_tg_fork = raw != "discovered" or master_added_at is not None
    # check/Δtg всегда видны (иначе на master running кажется, что проверки не будет)
    show_check_fork = True

    tg_state = _side_tg_state(
        pipeline_status=raw, tg_status=tg_status, tracked=tracked
    )
    if not show_tg_fork:
        tg_state = "skipped" if not tracked else "pending"
    delta_tg = _side_delta_tg_state(
        tracked=tracked, files_status=files_status, check_state=check_state
    )

    return {
        "main": main,
        "tg_fork": {
            "show": show_tg_fork,
            "tg": {"id": "tg", "label": "tg", "state": tg_state},
        },
        "check_fork": {
            "show": show_check_fork,
            "check": {"id": "check", "label": "check", "state": check_state},
            "delta_tg": {"id": "files", "label": "Δtg", "state": delta_tg},
        },
    }


def resolve_tracked_release_ids(db: Session, release_ids: list[int]) -> set[int]:
    """release_id с enabled tracked_releases."""
    ids = sorted({int(r) for r in release_ids if r is not None})
    if not ids:
        return set()
    from app.db.models import TrackedRelease

    rows = db.scalars(
        select(TrackedRelease.release_id).where(
            TrackedRelease.release_id.in_(ids),
            TrackedRelease.enabled.is_(True),
        )
    ).all()
    return {int(r) for r in rows}


def resolve_files_stage_statuses(
    db: Session,
    pipelines: list[TorrentPipeline],
) -> dict[int, str]:
    """pipeline_id → pending|running|synced|success|failed для стадии check.

    synced = ранний sync_composition (check ok, Δtg ещё нет).
    success = hash_done / hash_settle.
    hash_* решают раньше sync; sync не перекрывает hash_fail.
    Не помечает done как running без событий.
    """
    if not pipelines:
        return {}
    by_id = {p.id: "pending" for p in pipelines}
    ready_ids = [
        p.id
        for p in pipelines
        if (p.status or "") != "discovered" or p.master_added_at is not None
    ]
    if not ready_ids:
        return by_id

    rows = db.execute(
        select(
            PipelineEvent.pipeline_id,
            PipelineEvent.event_type,
            PipelineEvent.message,
            PipelineEvent.details_json,
            PipelineEvent.id,
        )
        .where(
            PipelineEvent.pipeline_id.in_(ready_ids),
            PipelineEvent.event_type.in_(sorted(_CHECK_EVENT_TYPES)),
        )
        .order_by(PipelineEvent.id.desc())
    ).all()

    # hash_* — терминальные; sync_composition не закрывает скан (не перекрывает hash_fail).
    decided: set[int] = set()
    for pipeline_id, event_type, message, details_json, _eid in rows:
        if pipeline_id in decided:
            continue
        et = (event_type or "").strip().lower()
        if et == "hash_fail":
            by_id[pipeline_id] = "failed"
            decided.add(pipeline_id)
        elif et == "hash_done":
            by_id[pipeline_id] = "success"
            decided.add(pipeline_id)
        elif et in {"hash_enqueued", "hash_progress"}:
            by_id[pipeline_id] = "running"
            decided.add(pipeline_id)
        elif et == "ui_status":
            details = details_json if isinstance(details_json, dict) else {}
            phase = str(details.get("phase") or "").strip().lower()
            msg = (message or "").lower()
            if phase == "hash_settle":
                by_id[pipeline_id] = "success"
                decided.add(pipeline_id)
            elif phase == "sync_composition" or "sync_composition" in msg:
                if by_id[pipeline_id] == "pending":
                    by_id[pipeline_id] = "synced"
                # не decided — ниже по id могут быть hash_*
    return by_id


def _qb_torrent_complete_state(progress: float, state: str) -> bool:
    """Торрент скачан и/или на раздаче (как classify_*_torrent → complete)."""
    if progress >= 1.0:
        return True
    return state in {
        "uploading",
        "stalledup",
        "queuedup",
        "forcedup",
        "pausedup",
        "stoppedup",
    }


def record_pipeline_event(
    db: Session,
    pipeline_id: int,
    *,
    event_type: str,
    message: str,
    job_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> PipelineEvent:
    """Записать событие audit trail (можно вызывать вне TorrentPipelineService)."""
    event = PipelineEvent(
        pipeline_id=pipeline_id,
        job_id=job_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        message=message,
        details_json=dict(details or {}),
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


class TorrentPipelineService:
    STATUS_DISCOVERED = "discovered"
    STATUS_WAITING_MASTER = "waiting_master"
    STATUS_MASTER_ADDED = "master_added"
    STATUS_MASTER_COMPLETE = "master_complete"
    STATUS_WAITING_SLAVE = "waiting_slave"
    STATUS_SLAVE_ADDED = "slave_added"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    # Успешный terminal: slave закончил и на раздаче.
    _TERMINAL_OK = frozenset({STATUS_DONE})
    # Досылка на slave уже состоялась (ещё качает / уже done).
    _SLAVE_REACHED = frozenset({STATUS_SLAVE_ADDED, STATUS_DONE})
    _AWAITING_SLAVE = frozenset({STATUS_MASTER_ADDED, STATUS_MASTER_COMPLETE})
    _EXCLUDED_FROM_LATEST = frozenset({STATUS_FAILED, STATUS_CANCELLED})

    def __init__(
        self,
        db: Session,
        job_id: int | None = None,
        *,
        actor: str | None = None,
    ) -> None:
        self._db = db
        self._job_id = job_id
        # job | webhook | poll | manual — пишется в details_json.actor
        if actor:
            self._actor = actor
        elif job_id is not None:
            self._actor = "job"
        else:
            self._actor = None
        self._master_client: qbittorrentapi.Client | None = None

    def _add_log(self, message: str, level: str = "info") -> None:
        if self._job_id is None:
            return
        self._db.add(JobLog(job_id=self._job_id, level=level, message=message))
        self._db.commit()

    def _record_event(
        self,
        pipeline: TorrentPipeline,
        *,
        event_type: str,
        message: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: dict[str, Any] | None = None,
        log_level: str = "info",
    ) -> None:
        """Всегда пишет pipeline_events; job_logs — только если есть job_id."""
        details_json = dict(details or {})
        if "actor" not in details_json and self._actor:
            details_json["actor"] = self._actor
        record_pipeline_event(
            self._db,
            pipeline.id,
            event_type=event_type,
            message=message,
            job_id=self._job_id,
            from_status=from_status,
            to_status=to_status,
            details=details_json,
            commit=False,
        )
        if self._job_id is not None:
            self._db.add(JobLog(job_id=self._job_id, level=log_level, message=message))
        self._db.commit()

    def create_discovered(self, info_hash: str, release_id: int, torrent_id: int) -> TorrentPipeline:
        pipeline = TorrentPipeline(
            info_hash=info_hash.lower(),
            release_id=release_id,
            torrent_id=torrent_id,
            status=self.STATUS_DISCOVERED,
        )
        self._db.add(pipeline)
        self._db.commit()
        self._db.refresh(pipeline)
        message = (
            f"Pipeline {pipeline.id} создан: release_id={release_id}, "
            f"torrent_id={torrent_id}, status={pipeline.status}"
        )
        self._record_event(
            pipeline,
            event_type="created",
            message=message,
            to_status=self.STATUS_DISCOVERED,
            details={"release_id": release_id, "torrent_id": torrent_id, "info_hash": pipeline.info_hash},
            log_level="debug",
        )
        return pipeline

    def get_latest_by_hash(self, info_hash: str) -> TorrentPipeline | None:
        """Последняя запись по hash. Failed/cancelled не перекрывают активные статусы."""
        normalized_hash = info_hash.strip().lower()
        active = self._db.scalar(
            select(TorrentPipeline)
            .where(
                TorrentPipeline.info_hash == normalized_hash,
                TorrentPipeline.status.not_in(self._EXCLUDED_FROM_LATEST),
            )
            .order_by(TorrentPipeline.id.desc())
            .limit(1)
        )
        if active is not None:
            return active
        return self._db.scalar(
            select(TorrentPipeline)
            .where(TorrentPipeline.info_hash == normalized_hash)
            .order_by(TorrentPipeline.id.desc())
            .limit(1)
        )

    def mark_waiting_master(
        self,
        pipeline: TorrentPipeline,
        reason: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        # Уже waiting_master: не спамим PipelineEvent на каждом retry.
        if pipeline.status == self.STATUS_WAITING_MASTER:
            if reason is not None and reason != pipeline.error:
                pipeline.error = reason
                self._db.commit()
                self._db.refresh(pipeline)
            return pipeline
        from_status = pipeline.status
        pipeline.status = self.STATUS_WAITING_MASTER
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        suffix = f": {reason}" if reason else ""
        event_details = dict(details or {})
        if reason:
            event_details["reason"] = reason
        self._record_event(
            pipeline,
            event_type="status_change",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}{suffix}",
            from_status=from_status,
            to_status=pipeline.status,
            details=event_details,
            log_level="warning",
        )
        return pipeline

    def mark_master_added(
        self,
        pipeline: TorrentPipeline,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_MASTER_ADDED
        pipeline.master_added_at = utcnow()
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="master_add",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}",
            from_status=from_status,
            to_status=pipeline.status,
            details=details,
            log_level="debug",
        )
        self._sync_composition_best_effort(pipeline)
        return pipeline

    def _sync_composition_best_effort(self, pipeline: TorrentPipeline) -> None:
        """Ранний sync состава на master_added — UI видит «новый» до hash_torrent."""
        from app.services.file_tracker import FileTrackerService

        try:
            torrent_bytes = self.load_torrent_bytes_from_archive(pipeline)
            if torrent_bytes is None:
                self._add_log(
                    f"Pipeline {pipeline.id}: sync состава пропуск — нет .torrent в архиве",
                    "warning",
                )
                return
            result = FileTrackerService(self._db).sync_torrent_composition(
                info_hash=pipeline.info_hash,
                torrent_id=pipeline.torrent_id,
                release_id=pipeline.release_id,
                torrent_bytes=torrent_bytes,
                # TG: events + уведомление — после hash_torrent, не во время закачки.
                notify=False,
            )
            if result.skipped_reason:
                self._add_log(
                    f"Pipeline {pipeline.id}: sync состава пропуск — {result.skipped_reason}",
                    "warning",
                )
                return
            added = sum(1 for c in result.changes if c.kind == "added")
            removed = sum(1 for c in result.changes if c.kind == "removed")
            self._add_log(
                f"Pipeline {pipeline.id}: sync состава files={result.files_upserted}, "
                f"added={added}, removed={removed} "
                f"(детали prior/ui_status — в логе sync_composition)",
                "info",
            )
        except Exception as exc:
            self._add_log(
                f"Pipeline {pipeline.id}: sync состава не удался: {exc}",
                "warning",
            )

    def mark_master_complete(
        self,
        pipeline: TorrentPipeline,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_MASTER_COMPLETE
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="status_change",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}",
            from_status=from_status,
            to_status=pipeline.status,
            details=details,
            log_level="debug",
        )
        return pipeline

    def mark_waiting_slave(
        self,
        pipeline: TorrentPipeline,
        reason: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        # Уже waiting_slave: не спамим PipelineEvent на каждом retry.
        if pipeline.status == self.STATUS_WAITING_SLAVE:
            if reason is not None and reason != pipeline.error:
                pipeline.error = reason
                self._db.commit()
                self._db.refresh(pipeline)
            return pipeline
        from_status = pipeline.status
        pipeline.status = self.STATUS_WAITING_SLAVE
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        suffix = f": {reason}" if reason else ""
        event_details = dict(details or {})
        if reason:
            event_details["reason"] = reason
        self._record_event(
            pipeline,
            event_type="status_change",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}{suffix}",
            from_status=from_status,
            to_status=pipeline.status,
            details=event_details,
            log_level="warning",
        )
        return pipeline

    def mark_slave_added(
        self,
        pipeline: TorrentPipeline,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_SLAVE_ADDED
        pipeline.slave_added_at = utcnow()
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="slave_add",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}",
            from_status=from_status,
            to_status=pipeline.status,
            details=details,
            log_level="debug",
        )
        return pipeline

    def mark_done(
        self,
        pipeline: TorrentPipeline,
        *,
        details: dict[str, Any] | None = None,
    ) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_DONE
        pipeline.slave_completed_at = utcnow()
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="status_change",
            message=f"Pipeline {pipeline.id} переведен в status={pipeline.status}",
            from_status=from_status,
            to_status=pipeline.status,
            details=details,
            log_level="debug",
        )
        return pipeline

    def mark_failed(self, pipeline: TorrentPipeline, error: str) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_FAILED
        pipeline.error = error
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="failed",
            message=f"Pipeline {pipeline.id} завершился ошибкой: {error}",
            from_status=from_status,
            to_status=pipeline.status,
            details={"error": error},
            log_level="error",
        )
        return pipeline

    def mark_cancelled(self, pipeline: TorrentPipeline, reason: str) -> TorrentPipeline:
        from_status = pipeline.status
        pipeline.status = self.STATUS_CANCELLED
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        self._record_event(
            pipeline,
            event_type="cancelled",
            message=f"Pipeline {pipeline.id} отменён: {reason}",
            from_status=from_status,
            to_status=pipeline.status,
            details={"reason": reason},
            log_level="warning",
        )
        return pipeline

    def get_waiting_slave_pipelines(self) -> list[TorrentPipeline]:
        rows = self._db.scalars(
            select(TorrentPipeline)
            .where(TorrentPipeline.status == self.STATUS_WAITING_SLAVE)
            .order_by(TorrentPipeline.id.asc())
        ).all()
        return list(rows)

    def get_waiting_master_pipelines(self) -> list[TorrentPipeline]:
        rows = self._db.scalars(
            select(TorrentPipeline)
            .where(TorrentPipeline.status == self.STATUS_WAITING_MASTER)
            .order_by(TorrentPipeline.id.asc())
        ).all()
        return list(rows)

    def get_failed_qb_wait_pipelines(self) -> list[TorrentPipeline]:
        """failed с connection/auth-подобной ошибкой — кандидаты на recovery."""
        rows = self._db.scalars(
            select(TorrentPipeline)
            .where(TorrentPipeline.status == self.STATUS_FAILED)
            .order_by(TorrentPipeline.id.asc())
        ).all()
        return [row for row in rows if is_qb_wait_error_text(row.error)]

    def get_master_added_older_than(self, minutes: int) -> list[TorrentPipeline]:
        threshold = utcnow() - timedelta(minutes=minutes)
        rows = self._db.scalars(
            select(TorrentPipeline).where(
                TorrentPipeline.status == self.STATUS_MASTER_ADDED,
                TorrentPipeline.master_added_at.is_not(None),
                TorrentPipeline.master_added_at <= threshold,
            )
        ).all()
        return list(rows)

    def get_slave_added_older_than(self, minutes: int) -> list[TorrentPipeline]:
        """Aged slave_added — fallback poll, пока slave не на раздаче."""
        threshold = utcnow() - timedelta(minutes=minutes)
        rows = self._db.scalars(
            select(TorrentPipeline).where(
                TorrentPipeline.status == self.STATUS_SLAVE_ADDED,
                TorrentPipeline.slave_added_at.is_not(None),
                TorrentPipeline.slave_added_at <= threshold,
            )
        ).all()
        return list(rows)

    def get_pipelines_awaiting_slave(self) -> list[TorrentPipeline]:
        """Pipeline, которых ещё нет на slave (ожидают callback / досылку)."""
        rows = self._db.scalars(
            select(TorrentPipeline)
            .where(TorrentPipeline.status.in_(self._AWAITING_SLAVE))
            .order_by(TorrentPipeline.id.asc())
        ).all()
        return list(rows)

    def _claim_master_complete(self, pipeline_id: int) -> TorrentPipeline | None:
        """Атомарный переход master_added → master_complete. None если статус уже другой."""
        claimed = self._db.scalar(
            update(TorrentPipeline)
            .where(
                TorrentPipeline.id == pipeline_id,
                TorrentPipeline.status == self.STATUS_MASTER_ADDED,
            )
            .values(status=self.STATUS_MASTER_COMPLETE, error=None)
            .returning(TorrentPipeline)
        )
        self._db.commit()
        if claimed is not None:
            self._record_event(
                claimed,
                event_type="status_change",
                message=f"Pipeline {claimed.id} переведен в status={claimed.status}",
                from_status=self.STATUS_MASTER_ADDED,
                to_status=self.STATUS_MASTER_COMPLETE,
                log_level="debug",
            )
        return claimed

    def process_completion(self, pipeline: TorrentPipeline, torrent_bytes: bytes) -> TorrentPipeline:
        """Идемпотентное завершение: master_added → slave; waiting_slave / master_complete — досылка.

        Останавливается на slave_added (done — отдельно через process_slave_completion).
        """
        self._db.refresh(pipeline)
        if pipeline.status in self._SLAVE_REACHED:
            self._record_event(
                pipeline,
                event_type="status_change",
                message=f"Pipeline {pipeline.id}: process_completion no-op, status={pipeline.status}",
                from_status=pipeline.status,
                to_status=pipeline.status,
                details={"noop": True},
                log_level="debug",
            )
            return pipeline
        if pipeline.status in {self.STATUS_MASTER_COMPLETE, self.STATUS_WAITING_SLAVE}:
            result = self._add_to_slave(pipeline, torrent_bytes)
            return self._after_slave_add(result)
        if pipeline.status != self.STATUS_MASTER_ADDED:
            raise RuntimeError(
                f"Pipeline {pipeline.id} нельзя завершить из status={pipeline.status}"
            )

        claimed = self._claim_master_complete(pipeline.id)
        if claimed is None:
            self._db.refresh(pipeline)
            if pipeline.status in self._SLAVE_REACHED:
                self._record_event(
                    pipeline,
                    event_type="status_change",
                    message=f"Pipeline {pipeline.id}: пропуск race/повтор, status={pipeline.status}",
                    from_status=pipeline.status,
                    to_status=pipeline.status,
                    details={"race": True},
                    log_level="debug",
                )
                # Даже на race: убедимся, что hash_torrent поставлен.
                self._enqueue_hash_torrent(pipeline)
                if pipeline.status == self.STATUS_SLAVE_ADDED:
                    return self.process_slave_completion(pipeline)
                return pipeline
            if pipeline.status in {self.STATUS_MASTER_COMPLETE, self.STATUS_WAITING_SLAVE}:
                result = self._add_to_slave(pipeline, torrent_bytes)
                return self._after_slave_add(result)
            raise RuntimeError(
                f"Pipeline {pipeline.id} нельзя завершить из status={pipeline.status}"
            )

        pipeline = claimed
        result = self._add_to_slave(pipeline, torrent_bytes)
        return self._after_slave_add(result)

    def _after_slave_add(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        """После add: hash сразу; если уже complete на slave — done."""
        self._enqueue_hash_torrent(pipeline)
        if pipeline.status == self.STATUS_SLAVE_ADDED:
            return self.process_slave_completion(pipeline)
        return pipeline

    def process_slave_completion(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        """slave_added + slave complete/seeding → done; missing → cancelled."""
        self._db.refresh(pipeline)
        if pipeline.status == self.STATUS_DONE:
            self._record_event(
                pipeline,
                event_type="status_change",
                message=f"Pipeline {pipeline.id}: process_slave_completion no-op, status=done",
                from_status=pipeline.status,
                to_status=pipeline.status,
                details={"noop": True},
                log_level="debug",
            )
            return pipeline
        if pipeline.status != self.STATUS_SLAVE_ADDED:
            self._record_event(
                pipeline,
                event_type="status_change",
                message=(
                    f"Pipeline {pipeline.id}: process_slave_completion no-op, "
                    f"status={pipeline.status}"
                ),
                from_status=pipeline.status,
                to_status=pipeline.status,
                details={"noop": True},
                log_level="debug",
            )
            return pipeline

        state = self.classify_slave_torrent(pipeline)
        if state == "in_progress":
            return pipeline
        if state == "missing":
            return self.mark_cancelled(
                pipeline,
                "Торрент отсутствует на slave (удалён) — pipeline cancelled",
            )
        return self.mark_done(pipeline, details={"qb_role": "slave", "slave_state": state})

    def _hash_torrent_already_done_or_queued(self, info_hash: str) -> bool:
        """Не дублировать hash_torrent.

        pending/running — уже в работе.
        success — идемпотентность после успешного прохода (мягкий skip api_present тоже success).
        failed/cancelled — можно поставить снова (например не было .torrent в архиве).
        """
        from app.db.models import Job
        from app.services.job_runner import STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS

        normalized = (info_hash or "").strip().lower()
        if not normalized:
            return False
        rows = self._db.scalars(
            select(Job).where(
                Job.type == "hash_torrent",
                Job.status.in_((STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS)),
            )
        ).all()
        for job in rows:
            params = job.params_json or {}
            if str(params.get("info_hash") or "").strip().lower() == normalized:
                return True
        return False

    def _enqueue_hash_torrent(self, pipeline: TorrentPipeline) -> None:
        """Фоновый hash_torrent; не дублирует pending/running/success (failed — можно снова)."""
        from app.api.rest import job_runner
        from app.db.models import TorrentArchive
        from app.services.job_runner import JobAlreadyRunningError, UnknownJobTypeError

        archive = self._db.scalar(
            select(TorrentArchive)
            .where(
                (TorrentArchive.info_hash == pipeline.info_hash)
                | (TorrentArchive.torrent_id == pipeline.torrent_id)
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if archive is not None and not archive.api_present:
            # Не пишем PipelineEvent — job не поставлен; иначе spam на каждый retry.
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent пропуск (api_present=false)",
                "debug",
            )
            return
        if self._hash_torrent_already_done_or_queued(pipeline.info_hash):
            # Не пишем PipelineEvent — job не поставлен; иначе spam на каждый retry.
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent уже был/в очереди — пропуск",
                "debug",
            )
            return
        job = None
        try:
            job = job_runner.create_job(
                self._db,
                "hash_torrent",
                {
                    "info_hash": pipeline.info_hash,
                    "torrent_id": pipeline.torrent_id,
                    "release_id": pipeline.release_id,
                },
            )
            try:
                job_runner.schedule_job(job.id)
            except Exception as schedule_exc:
                # Иначе pending навсегда блокирует повторный enqueue для info_hash.
                self._mark_hash_job_schedule_failed(job.id, schedule_exc)
                raise
            self._record_event(
                pipeline,
                event_type="hash_enqueued",
                message=f"Pipeline {pipeline.id}: поставлен hash_torrent job_id={job.id}",
                details={"hash_job_id": job.id},
                log_level="debug",
            )
        except JobAlreadyRunningError as exc:
            # Не пишем PipelineEvent — job уже в очереди, повторный enqueue не состоялся.
            self._add_log(
                (
                    f"Pipeline {pipeline.id}: hash_torrent уже в очереди "
                    f"(job_id={exc.running_job_id})"
                ),
                "debug",
            )
        except UnknownJobTypeError:
            self._record_event(
                pipeline,
                event_type="hash_enqueued",
                message=f"Pipeline {pipeline.id}: hash_torrent не зарегистрирован",
                details={"error": "unknown_job_type"},
                log_level="warning",
            )
        except Exception as exc:
            self._record_event(
                pipeline,
                event_type="hash_enqueued",
                message=f"Pipeline {pipeline.id}: не удалось поставить hash_torrent: {exc}",
                details={"error": str(exc)},
                log_level="warning",
            )

    def _mark_hash_job_schedule_failed(self, job_id: int, schedule_exc: Exception) -> None:
        """Пометить pending hash_torrent как failed; при сбое commit — retry после rollback."""
        from app.db.models import Job
        from app.services.job_runner import STATUS_FAILED, STATUS_RUNNING

        error_text = f"не удалось запланировать выполнение: {schedule_exc}"[:500]

        def _apply(row: Job) -> None:
            if row.status == STATUS_RUNNING:
                return
            row.status = STATUS_FAILED
            row.error = error_text
            if row.finished_at is None:
                row.finished_at = utcnow()

        for attempt in (1, 2):
            try:
                if attempt == 2:
                    try:
                        self._db.rollback()
                    except Exception:
                        pass
                row = self._db.get(Job, job_id)
                if row is not None:
                    _apply(row)
                    self._db.commit()
                return
            except Exception as mark_exc:
                if attempt == 1:
                    self._add_log(
                        f"hash_torrent job_id={job_id}: не удалось пометить failed ({mark_exc}), retry",
                        "warning",
                    )
                    continue
                self._add_log(
                    f"hash_torrent job_id={job_id}: повторная пометка failed не удалась: {mark_exc}",
                    "error",
                )

    def _add_to_slave(self, pipeline: TorrentPipeline, torrent_bytes: bytes) -> TorrentPipeline:
        try:
            slave = self._get_qb_client("slave")
            if slave is None:
                return self.mark_waiting_slave(pipeline, qb_client_wait_message("slave", missing=True))
            qb = qbittorrentapi.Client(
                host=slave.host,
                port=slave.port,
                username=slave.username,
                password=slave.password_encrypted,
            )
            qb.auth_log_in()

            passkey = get_setting_value(self._db, "anilibria_passkey", "")
            prepared = ensure_announce_passkey(torrent_bytes, passkey)
            rename, comment, category, tags = self._resolve_qb_meta(pipeline)

            added_new, comment_ok, tags_ok = qb_add_torrent(
                qb,
                prepared,
                rename=rename,
                comment=comment,
                category=category,
                tags=tags,
            )
            if comment and not comment_ok:
                self._add_log(
                    f"Pipeline {pipeline.id}: comment не установлен на slave "
                    f"(torrent_id={pipeline.torrent_id})",
                    "warning",
                )
            if tags and not tags_ok:
                self._add_log(
                    f"Pipeline {pipeline.id}: tags не установлены на slave "
                    f"(torrent_id={pipeline.torrent_id}, tags={tags})",
                    "warning",
                )
            if added_new:
                self._add_log(
                    f"Pipeline {pipeline.id}: torrent_id={pipeline.torrent_id} добавлен в slave",
                    "debug",
                )
            else:
                self._add_log(
                    f"Pipeline {pipeline.id}: torrent_id={pipeline.torrent_id} уже есть в slave (Conflict)",
                    "debug",
                )
            slave_details = {"added_new": added_new, "qb_role": "slave"}
            # Имя раздачи (meta), не метка QbClient.
            if rename:
                slave_details["qb_name"] = rename
            return self.mark_slave_added(pipeline, details=slave_details)
        except Exception as exc:
            if should_wait_for_qb(exc):
                return self.mark_waiting_slave(pipeline, qb_client_wait_message("slave", exc))
            self._db.refresh(pipeline)
            raise

    def _resolve_qb_meta(
        self, pipeline: TorrentPipeline
    ) -> tuple[str | None, str | None, str | None, list[str]]:
        archive = self._db.scalar(
            select(TorrentArchive)
            .where(
                (TorrentArchive.info_hash == pipeline.info_hash)
                | (TorrentArchive.torrent_id == pipeline.torrent_id)
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        rename: str | None = None
        category: str | None = None
        tags: list[str] = []
        alias_candidates: list[str | None] = []

        if archive is not None:
            rename = build_qb_torrent_name_from_archive(
                anime_name=archive.anime_name,
                torrent_description=archive.torrent_description,
                torrent_type=archive.torrent_type,
                quality_json=archive.quality_json if isinstance(archive.quality_json, dict) else None,
            )
            category = archive.category
            alias_candidates.append(archive.release_alias)
            tags = genres_from_quality_json(
                archive.quality_json if isinstance(archive.quality_json, dict) else None
            )

        sibling_alias = self._db.scalar(
            select(TorrentArchive.release_alias)
            .where(
                TorrentArchive.release_id == pipeline.release_id,
                TorrentArchive.release_alias.isnot(None),
                TorrentArchive.release_alias != "",
            )
            .limit(1)
        )
        if isinstance(sibling_alias, str):
            alias_candidates.append(sibling_alias)

        if not tags:
            sibling = self._db.scalar(
                select(TorrentArchive)
                .where(
                    TorrentArchive.release_id == pipeline.release_id,
                    TorrentArchive.quality_json.isnot(None),
                )
                .order_by(TorrentArchive.id.desc())
                .limit(1)
            )
            if sibling is not None:
                tags = genres_from_quality_json(
                    sibling.quality_json if isinstance(sibling.quality_json, dict) else None
                )

        extra_alias = self._db.scalar(
            select(ExtraUrl.release_alias)
            .where(
                ExtraUrl.release_id == pipeline.release_id,
                ExtraUrl.enabled.is_(True),
            )
            .limit(1)
        )
        if isinstance(extra_alias, str):
            alias_candidates.append(extra_alias)

        comment = first_release_torrents_url(
            *alias_candidates,
            site_url=resolve_anilibria_site_url(),
        )
        if comment is None:
            self._add_log(
                f"Pipeline {pipeline.id}: нет alias для comment "
                f"(release_id={pipeline.release_id}, torrent_id={pipeline.torrent_id})",
                "warning",
            )
        return rename, comment, category, tags

    def load_torrent_bytes_from_archive(self, pipeline: TorrentPipeline) -> bytes | None:
        """Берёт .torrent из локального архива (предпочтительно для slave)."""
        archive = self._db.scalar(
            select(TorrentArchive)
            .where(
                (TorrentArchive.info_hash == pipeline.info_hash)
                | (TorrentArchive.torrent_id == pipeline.torrent_id)
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if archive is None:
            return None
        path = TorrentArchiveService(self._db).resolve_file_path(archive)
        if not path.exists():
            return None
        passkey = get_setting_value(self._db, "anilibria_passkey", "")
        return ensure_announce_passkey(path.read_bytes(), passkey)

    def _get_master_api(self) -> qbittorrentapi.Client:
        if self._master_client is not None:
            return self._master_client
        master = self._get_qb_client("master")
        if master is None:
            raise RuntimeError("Не найден активный qBittorrent клиент с ролью master")
        qb = qbittorrentapi.Client(
            host=master.host,
            port=master.port,
            username=master.username,
            password=master.password_encrypted,
        )
        qb.auth_log_in()
        self._master_client = qb
        return qb

    def _get_slave_api(self) -> qbittorrentapi.Client:
        slave = self._get_qb_client("slave")
        if slave is None:
            raise RuntimeError("Не найден активный qBittorrent клиент с ролью slave")
        qb = qbittorrentapi.Client(
            host=slave.host,
            port=slave.port,
            username=slave.username,
            password=slave.password_encrypted,
        )
        qb.auth_log_in()
        return qb

    def classify_master_torrent(self, pipeline: TorrentPipeline) -> MasterTorrentState:
        """Статус торрента на master: complete / in_progress / missing."""
        qb = self._get_master_api()
        torrents = qb.torrents_info(hashes=pipeline.info_hash)
        if not torrents:
            return "missing"
        torrent = torrents[0]
        progress = float(getattr(torrent, "progress", 0.0) or 0.0)
        state = str(getattr(torrent, "state", "") or "").lower()
        if _qb_torrent_complete_state(progress, state):
            return "complete"
        return "in_progress"

    def classify_slave_torrent(self, pipeline: TorrentPipeline) -> SlaveTorrentState:
        """Статус торрента на slave: complete / in_progress / missing."""
        qb = self._get_slave_api()
        torrents = qb.torrents_info(hashes=pipeline.info_hash)
        if not torrents:
            return "missing"
        torrent = torrents[0]
        progress = float(getattr(torrent, "progress", 0.0) or 0.0)
        state = str(getattr(torrent, "state", "") or "").lower()
        if _qb_torrent_complete_state(progress, state):
            return "complete"
        return "in_progress"

    def is_master_torrent_complete(self, pipeline: TorrentPipeline) -> bool:
        return self.classify_master_torrent(pipeline) == "complete"

    def get_master_ui_states(self, info_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Пакетный опрос master: hash → {key, label, progress, raw_state}."""
        return self._get_qb_ui_states(info_hashes, role="master")

    def get_slave_ui_states(self, info_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Пакетный опрос slave: hash → {key, label, progress, raw_state}."""
        return self._get_qb_ui_states(info_hashes, role="slave")

    def _get_qb_ui_states(
        self, info_hashes: list[str], *, role: Literal["master", "slave"]
    ) -> dict[str, dict[str, Any]]:
        labels = MASTER_UI_LABELS if role == "master" else SLAVE_UI_LABELS
        normalized = sorted({(h or "").strip().lower() for h in info_hashes if (h or "").strip()})
        if not normalized:
            return {}

        unavailable = {
            "key": "unavailable",
            "label": labels["unavailable"],
            "progress": None,
            "raw_state": None,
        }
        try:
            qb = self._get_master_api() if role == "master" else self._get_slave_api()
            torrents = qb.torrents_info(hashes="|".join(normalized))
        except Exception:
            return {h: dict(unavailable) for h in normalized}

        by_hash: dict[str, Any] = {}
        for torrent in torrents or []:
            raw_hash = str(getattr(torrent, "hash", "") or "").strip().lower()
            if not raw_hash:
                continue
            progress = float(getattr(torrent, "progress", 0.0) or 0.0)
            raw_state = str(getattr(torrent, "state", "") or "")
            key = map_qb_torrent_ui_state(raw_state, progress)
            by_hash[raw_hash] = {
                "key": key,
                "label": labels.get(key, key),
                "progress": progress,
                "raw_state": raw_state,
            }

        missing = {
            "key": "missing",
            "label": labels["missing"],
            "progress": None,
            "raw_state": None,
        }
        return {h: by_hash.get(h, missing) for h in normalized}

    def reconcile_with_master(
        self,
        *,
        load_torrent_bytes,
    ) -> dict[str, Any]:
        """Сверка pipeline без slave с master (пропущенный webhook).

        - complete → досылка на slave
        - in_progress → ждём callback
        - missing → cancelled
        - failed (connection-like) + торрент на master → recovery в master_added / slave
        """
        stats: dict[str, Any] = {
            "checked": 0,
            "sent_to_slave": 0,
            "waiting": 0,
            "cancelled": 0,
            "recovered": 0,
            "errors": 0,
            "details": [],
        }
        candidates = self.get_pipelines_awaiting_slave()
        failed_recoverable = self.get_failed_qb_wait_pipelines()
        for pipeline in failed_recoverable:
            stats["checked"] += 1
            try:
                state = self.classify_master_torrent(pipeline)
                if state == "missing":
                    # Ещё нет на master — waiting_master_retry дошлёт add.
                    stats["waiting"] += 1
                    stats["details"].append(
                        {
                            "id": pipeline.id,
                            "hash": pipeline.info_hash,
                            "action": "failed_await_master_retry",
                        }
                    )
                    continue
                self.mark_master_added(pipeline)
                stats["recovered"] += 1
                if state == "in_progress":
                    stats["waiting"] += 1
                    stats["details"].append(
                        {
                            "id": pipeline.id,
                            "hash": pipeline.info_hash,
                            "action": "recovered_master_added",
                        }
                    )
                    continue
                candidates = [*candidates, pipeline]
            except Exception as exc:
                if should_wait_for_qb(exc):
                    stats["waiting"] += 1
                    stats["details"].append(
                        {
                            "id": pipeline.id,
                            "hash": pipeline.info_hash,
                            "action": "waiting_qb",
                            "error": str(exc),
                        }
                    )
                    continue
                stats["errors"] += 1
                stats["details"].append(
                    {
                        "id": pipeline.id,
                        "hash": pipeline.info_hash,
                        "action": "error",
                        "error": str(exc),
                    }
                )

        for pipeline in candidates:
            stats["checked"] += 1
            try:
                state = self.classify_master_torrent(pipeline)
                if state == "in_progress":
                    stats["waiting"] += 1
                    stats["details"].append(
                        {"id": pipeline.id, "hash": pipeline.info_hash, "action": "waiting"}
                    )
                    continue
                if state == "missing":
                    reason = "Торрент отсутствует на master (удалён) — pipeline cancelled"
                    self.mark_cancelled(pipeline, reason)
                    stats["cancelled"] += 1
                    stats["details"].append(
                        {"id": pipeline.id, "hash": pipeline.info_hash, "action": "cancelled"}
                    )
                    continue

                torrent_bytes = load_torrent_bytes(pipeline)
                if torrent_bytes is None:
                    raise RuntimeError("Нет .torrent в архиве и не удалось загрузить файл")
                updated = self.process_completion(pipeline, torrent_bytes)
                if updated.status == self.STATUS_WAITING_SLAVE:
                    stats["waiting"] += 1
                    stats["details"].append(
                        {"id": pipeline.id, "hash": pipeline.info_hash, "action": "waiting_slave"}
                    )
                else:
                    stats["sent_to_slave"] += 1
                    stats["details"].append(
                        {"id": pipeline.id, "hash": pipeline.info_hash, "action": "sent_to_slave"}
                    )
            except Exception as exc:
                if should_wait_for_qb(exc):
                    stats["waiting"] += 1
                    stats["details"].append(
                        {
                            "id": pipeline.id,
                            "hash": pipeline.info_hash,
                            "action": "waiting_qb",
                            "error": str(exc),
                        }
                    )
                    continue
                stats["errors"] += 1
                stats["details"].append(
                    {
                        "id": pipeline.id,
                        "hash": pipeline.info_hash,
                        "action": "error",
                        "error": str(exc),
                    }
                )
                try:
                    self.mark_failed(pipeline, f"Reconcile: {exc}")
                except Exception:
                    self._db.rollback()
        return stats

    def _get_qb_client(self, role: str) -> QbClient | None:
        return self._db.scalar(select(QbClient).where(QbClient.role == role, QbClient.enabled.is_(True)).limit(1))
