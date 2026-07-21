from datetime import datetime, timedelta
from typing import Any, Literal

import qbittorrentapi
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import ExtraUrl, JobLog, QbClient, TorrentArchive, TorrentPipeline
from app.services.qbittorrent import (
    MASTER_UI_LABELS,
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

    _TERMINAL_OK = frozenset({STATUS_DONE, STATUS_SLAVE_ADDED})
    _AWAITING_SLAVE = frozenset({STATUS_MASTER_ADDED, STATUS_MASTER_COMPLETE})
    _EXCLUDED_FROM_LATEST = frozenset({STATUS_FAILED, STATUS_CANCELLED})

    def __init__(self, db: Session, job_id: int | None = None) -> None:
        self._db = db
        self._job_id = job_id
        self._master_client: qbittorrentapi.Client | None = None

    def _add_log(self, message: str, level: str = "info") -> None:
        if self._job_id is None:
            return
        self._db.add(JobLog(job_id=self._job_id, level=level, message=message))
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
        self._add_log(
            f"Pipeline {pipeline.id} создан: release_id={release_id}, torrent_id={torrent_id}, status={pipeline.status}",
            "debug",
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

    def mark_waiting_master(self, pipeline: TorrentPipeline, reason: str | None = None) -> TorrentPipeline:
        pipeline.status = self.STATUS_WAITING_MASTER
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        suffix = f": {reason}" if reason else ""
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}{suffix}", "warning")
        return pipeline

    def mark_master_added(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        pipeline.status = self.STATUS_MASTER_ADDED
        pipeline.master_added_at = datetime.utcnow()
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}", "debug")
        return pipeline

    def mark_master_complete(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        pipeline.status = self.STATUS_MASTER_COMPLETE
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}", "debug")
        return pipeline

    def mark_waiting_slave(self, pipeline: TorrentPipeline, reason: str | None = None) -> TorrentPipeline:
        pipeline.status = self.STATUS_WAITING_SLAVE
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        suffix = f": {reason}" if reason else ""
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}{suffix}", "warning")
        return pipeline

    def mark_slave_added(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        pipeline.status = self.STATUS_SLAVE_ADDED
        pipeline.slave_added_at = datetime.utcnow()
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}", "debug")
        return pipeline

    def mark_done(self, pipeline: TorrentPipeline) -> TorrentPipeline:
        pipeline.status = self.STATUS_DONE
        pipeline.error = None
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}", "debug")
        return pipeline

    def mark_failed(self, pipeline: TorrentPipeline, error: str) -> TorrentPipeline:
        pipeline.status = self.STATUS_FAILED
        pipeline.error = error
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} завершился ошибкой: {error}", "error")
        return pipeline

    def mark_cancelled(self, pipeline: TorrentPipeline, reason: str) -> TorrentPipeline:
        pipeline.status = self.STATUS_CANCELLED
        pipeline.error = reason
        self._db.commit()
        self._db.refresh(pipeline)
        self._add_log(f"Pipeline {pipeline.id} отменён: {reason}", "warning")
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
        threshold = datetime.utcnow() - timedelta(minutes=minutes)
        rows = self._db.scalars(
            select(TorrentPipeline).where(
                TorrentPipeline.status == self.STATUS_MASTER_ADDED,
                TorrentPipeline.master_added_at.is_not(None),
                TorrentPipeline.master_added_at <= threshold,
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
        return claimed

    def process_completion(self, pipeline: TorrentPipeline, torrent_bytes: bytes) -> TorrentPipeline:
        """Идемпотентное завершение: master_added → slave; waiting_slave / master_complete — досылка."""
        self._db.refresh(pipeline)
        if pipeline.status in self._TERMINAL_OK:
            self._add_log(
                f"Pipeline {pipeline.id}: process_completion no-op, status={pipeline.status}",
                "debug",
            )
            return pipeline
        if pipeline.status in {self.STATUS_MASTER_COMPLETE, self.STATUS_WAITING_SLAVE}:
            result = self._add_to_slave(pipeline, torrent_bytes)
            # Повтор: поставить hash, если earlier failed/cancelled или ещё не ставили.
            # Уже success / pending / running — не дублируем.
            self._enqueue_hash_torrent(result)
            return result
        if pipeline.status != self.STATUS_MASTER_ADDED:
            raise RuntimeError(
                f"Pipeline {pipeline.id} нельзя завершить из status={pipeline.status}"
            )

        claimed = self._claim_master_complete(pipeline.id)
        if claimed is None:
            self._db.refresh(pipeline)
            if pipeline.status in self._TERMINAL_OK:
                self._add_log(
                    f"Pipeline {pipeline.id}: пропуск race/повтор, status={pipeline.status}",
                    "debug",
                )
                # Даже на race: убедимся, что hash_torrent поставлен.
                self._enqueue_hash_torrent(pipeline)
                return pipeline
            if pipeline.status in {self.STATUS_MASTER_COMPLETE, self.STATUS_WAITING_SLAVE}:
                result = self._add_to_slave(pipeline, torrent_bytes)
                self._enqueue_hash_torrent(result)
                return result
            raise RuntimeError(
                f"Pipeline {pipeline.id} нельзя завершить из status={pipeline.status}"
            )

        pipeline = claimed
        self._add_log(f"Pipeline {pipeline.id} переведен в status={pipeline.status}", "debug")
        result = self._add_to_slave(pipeline, torrent_bytes)
        self._enqueue_hash_torrent(result)
        return result

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
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent пропуск (api_present=false)",
                "debug",
            )
            return
        if self._hash_torrent_already_done_or_queued(pipeline.info_hash):
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent уже был/в очереди — пропуск",
                "debug",
            )
            return
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
            job_runner.schedule_job(job.id)
            self._add_log(
                f"Pipeline {pipeline.id}: поставлен hash_torrent job_id={job.id}",
                "debug",
            )
        except JobAlreadyRunningError as exc:
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent уже в очереди (job_id={exc.running_job_id})",
                "debug",
            )
        except UnknownJobTypeError:
            self._add_log(
                f"Pipeline {pipeline.id}: hash_torrent не зарегистрирован",
                "warning",
            )
        except Exception as exc:
            self._add_log(
                f"Pipeline {pipeline.id}: не удалось поставить hash_torrent: {exc}",
                "warning",
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
            self.mark_slave_added(pipeline)
            return self.mark_done(pipeline)
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

    def classify_master_torrent(self, pipeline: TorrentPipeline) -> MasterTorrentState:
        """Статус торрента на master: complete / in_progress / missing."""
        qb = self._get_master_api()
        torrents = qb.torrents_info(hashes=pipeline.info_hash)
        if not torrents:
            return "missing"
        torrent = torrents[0]
        progress = float(getattr(torrent, "progress", 0.0) or 0.0)
        state = str(getattr(torrent, "state", "") or "").lower()
        if progress >= 1.0 or state in {
            "uploading",
            "stalledup",
            "queuedup",
            "forcedup",
            "pausedup",
            "stoppedup",
        }:
            return "complete"
        return "in_progress"

    def is_master_torrent_complete(self, pipeline: TorrentPipeline) -> bool:
        return self.classify_master_torrent(pipeline) == "complete"

    def get_master_ui_states(self, info_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Пакетный опрос master: hash → {key, label, progress, raw_state}."""
        normalized = sorted({(h or "").strip().lower() for h in info_hashes if (h or "").strip()})
        if not normalized:
            return {}

        unavailable = {
            "key": "unavailable",
            "label": MASTER_UI_LABELS["unavailable"],
            "progress": None,
            "raw_state": None,
        }
        try:
            qb = self._get_master_api()
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
                "label": MASTER_UI_LABELS.get(key, key),
                "progress": progress,
                "raw_state": raw_state,
            }

        missing = {
            "key": "missing",
            "label": MASTER_UI_LABELS["missing"],
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
