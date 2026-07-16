from datetime import datetime
from typing import Any

import qbittorrentapi
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import ExtraUrl, JobLog, QbClient, SeenTorrent, TorrentArchive
from app.providers.anilibria.client import AniLibriaClient
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    _ensure_torrent_comment,
    collect_client_info_hashes,
    ensure_announce_passkey,
    qb_add_torrent,
    qb_client_wait_message,
    sanitize_info_hash,
    should_wait_for_qb,
    torrent_info_hash,
)
from app.services.release_checkpoint import (
    extract_release_markers,
    invalidate_release_checkpoint,
    mark_release_processed,
    should_skip_by_torrents_fingerprint,
    torrents_fingerprint,
)
from app.services.torrent_archive import TorrentArchiveService
from app.services.torrent_qb_meta import (
    build_qb_torrent_name_from_payloads,
    build_release_torrents_url,
    resolve_anilibria_site_url,
)


class TorrentProcessor:
    def __init__(self, db: Session, job_id: int, client: AniLibriaClient) -> None:
        self._db = db
        self._job_id = job_id
        self._al_client = client
        self._pipeline = TorrentPipelineService(db, job_id=job_id)
        self._master_wait_reason = qb_client_wait_message("master", missing=True)

    def _add_log(self, message: str, level: str = "info") -> None:
        self._db.add(JobLog(job_id=self._job_id, message=message, level=level))
        self._db.commit()

    def _get_master_client(self) -> QbClient:
        qb_client = self._db.scalar(select(QbClient).where(QbClient.role == "master", QbClient.enabled.is_(True)).limit(1))
        if qb_client is None:
            raise RuntimeError("Не найден активный qBittorrent клиент с ролью master")
        return qb_client

    def _try_connect_master(self) -> qbittorrentapi.Client | None:
        """Подключение к master. None если клиент лежит / auth / нет конфига."""
        return self._try_connect_qb("master")

    def _try_connect_qb(self, role: str, *, log_missing: bool = True) -> qbittorrentapi.Client | None:
        """Подключение к qB по роли. None если недоступен."""
        role_key = (role or "").strip().lower()
        try:
            row = self._db.scalar(
                select(QbClient).where(QbClient.role == role_key, QbClient.enabled.is_(True)).limit(1)
            )
            if row is None:
                if log_missing and role_key == "master":
                    self._master_wait_reason = qb_client_wait_message("master", missing=True)
                    self._add_log(
                        f"{self._master_wait_reason} Торренты → waiting_master.",
                        "warning",
                    )
                return None
            qb = qbittorrentapi.Client(
                host=row.host,
                port=row.port,
                username=row.username,
                password=row.password_encrypted,
            )
            qb.auth_log_in()
            return qb
        except Exception as exc:
            if role_key == "master" and should_wait_for_qb(exc):
                self._master_wait_reason = qb_client_wait_message("master", exc)
                self._add_log(
                    f"{self._master_wait_reason} Торренты → waiting_master.",
                    "warning",
                )
                return None
            if role_key == "master" and "Не найден" in str(exc):
                return None
            if role_key != "master" and should_wait_for_qb(exc):
                self._add_log(f"{qb_client_wait_message(role_key, exc)} (meta refresh)", "warning")
                return None
            if role_key == "master":
                raise
            self._add_log(f"qB {role_key}: {exc} (meta refresh)", "warning")
            return None

    def _qb_clients_for_meta(self) -> list[tuple[str, qbittorrentapi.Client]]:
        clients: list[tuple[str, qbittorrentapi.Client]] = []
        master = self._try_connect_qb("master", log_missing=False)
        if master is not None:
            clients.append(("master", master))
        slave = self._try_connect_qb("slave", log_missing=False)
        if slave is not None:
            clients.append(("slave", slave))
        return clients

    def _alias_for_release(self, release_id: int, release_alias: str | None) -> str | None:
        cleaned = (release_alias or "").strip().strip("/") or None
        if cleaned:
            return cleaned
        archived = self._db.scalar(
            select(TorrentArchive.release_alias)
            .where(
                TorrentArchive.release_id == release_id,
                TorrentArchive.release_alias.isnot(None),
            )
            .limit(1)
        )
        if isinstance(archived, str):
            text = archived.strip().strip("/")
            if text:
                return text
        extra = self._db.scalar(
            select(ExtraUrl.release_alias)
            .where(
                ExtraUrl.release_id == release_id,
                ExtraUrl.enabled.is_(True),
            )
            .limit(1)
        )
        if isinstance(extra, str):
            text = extra.strip().strip("/")
            return text or None
        return None

    def _candidate_hashes_for_torrent(
        self,
        *,
        torrent_id: int | None,
        api_hash: str | None,
        seen: SeenTorrent | None,
    ) -> list[str]:
        """Хэши для setComment: архив (из файла) → seen → API."""
        candidates: list[str] = []

        def _add(raw: str | None) -> None:
            if not raw:
                return
            try:
                value = sanitize_info_hash(raw)
            except ValueError:
                return
            if value not in candidates:
                candidates.append(value)

        if torrent_id is not None:
            for row in self._db.scalars(
                select(TorrentArchive.info_hash).where(TorrentArchive.torrent_id == torrent_id)
            ).all():
                _add(row)
        if seen is not None:
            _add(getattr(seen, "info_hash", None))
        _add(api_hash)
        return candidates

    def _refresh_qb_comments(
        self,
        *,
        release_id: int,
        release_alias: str | None,
        torrents: list[dict[str, Any]],
    ) -> int:
        """Проставляет URL релиза в comment на master/slave для уже известных торрентов."""
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        alias = self._alias_for_release(release_id, release_alias)
        release_url = build_release_torrents_url(alias, site_url=site_url)
        if not release_url:
            self._add_log(
                f"Релиз {release_id}: нет alias — нечего писать в comment",
                "warning",
            )
            return 0

        clients = self._qb_clients_for_meta()
        if not clients:
            self._add_log(
                f"Релиз {release_id}: нет доступных qB для обновления comment",
                "warning",
            )
            return 0

        updated = 0
        for torrent in torrents:
            torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            raw_hash = torrent.get("info_hash") or torrent.get("hash")
            api_hash = self._normalize_api_info_hash(raw_hash)
            seen = None
            if torrent_id is not None:
                seen = self._db.scalar(self.build_seen_exists_query(torrent_id, api_hash).limit(1))
            hashes = self._candidate_hashes_for_torrent(
                torrent_id=torrent_id,
                api_hash=api_hash,
                seen=seen,
            )
            if not hashes:
                continue

            for role, client in clients:
                ok = False
                for info_hash in hashes:
                    if _ensure_torrent_comment(
                        client,
                        info_hash,
                        release_url,
                        attempts=3,
                        delay_sec=0.2,
                        require_present=True,
                    ):
                        updated += 1
                        ok = True
                        break
                if not ok:
                    self._add_log(
                        f"Релиз {release_id}: не удалось обновить comment на {role} "
                        f"для torrent_id={torrent_id}",
                        "debug",
                    )
        if updated:
            self._add_log(
                f"Релиз {release_id}: обновлено comment на qB: {updated} "
                f"(url={release_url})",
                "debug",
            )
        return updated

    def backfill_qb_comments_from_archive(self) -> dict[str, int]:
        """Массово проставляет URL из torrent_archive на master/slave (после full_sync)."""
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        clients = self._qb_clients_for_meta()
        if not clients:
            self._add_log("Backfill comments: нет доступных qB", "warning")
            return {"archives": 0, "updated": 0, "missing": 0}

        present_by_role: dict[str, set[str] | None] = {}
        for role, client in clients:
            try:
                present_by_role[role] = collect_client_info_hashes(client)
                self._add_log(
                    f"Backfill comments: на {role} торрентов с hash={len(present_by_role[role] or [])}",
                    "debug",
                )
            except Exception as exc:
                present_by_role[role] = None
                self._add_log(
                    f"Backfill comments: не удалось получить список с {role}: {exc} — "
                    "будем пробовать по одному",
                    "warning",
                )

        archives = self._db.scalars(
            select(TorrentArchive).where(TorrentArchive.release_alias.isnot(None))
        ).all()
        updated = 0
        missing = 0
        for archive in archives:
            release_url = build_release_torrents_url(archive.release_alias, site_url=site_url)
            if not release_url:
                continue
            try:
                info_hash = sanitize_info_hash(archive.info_hash)
            except ValueError:
                continue

            for role, client in clients:
                present = present_by_role.get(role)
                if present is not None and info_hash not in present:
                    missing += 1
                    continue
                if _ensure_torrent_comment(
                    client,
                    info_hash,
                    release_url,
                    attempts=2,
                    delay_sec=0.15,
                    require_present=present is None,
                ):
                    updated += 1
                else:
                    missing += 1

        self._add_log(
            f"Backfill comments: готово, архивов={len(archives)}, "
            f"обновлено={updated}, пропущено/нет в qB={missing}"
        )
        return {"archives": len(archives), "updated": updated, "missing": missing}
    @staticmethod
    def _iter_torrents(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("list", "items", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_release_alias(release_payload: dict[str, Any]) -> str | None:
        alias = release_payload.get("alias")
        if isinstance(alias, str):
            cleaned = alias.strip()
            return cleaned or None
        return None

    @staticmethod
    def _normalize_api_info_hash(raw: Any) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return sanitize_info_hash(text)
        except ValueError:
            return None

    @staticmethod
    def build_seen_exists_query(torrent_id: int, info_hash: str | None):
        """Дедуп: тот же torrent_id ИЛИ тот же info_hash (если hash есть)."""
        if info_hash:
            return select(SeenTorrent).where(
                or_(SeenTorrent.torrent_id == torrent_id, SeenTorrent.info_hash == info_hash)
            )
        return select(SeenTorrent).where(SeenTorrent.torrent_id == torrent_id)

    def _mark_seen(self, *, torrent_id: int, info_hash: str, release_id: int) -> None:
        exists = self._db.scalar(self.build_seen_exists_query(torrent_id, info_hash).limit(1))
        if exists is not None:
            return
        self._db.add(
            SeenTorrent(
                torrent_id=torrent_id,
                info_hash=info_hash,
                release_id=release_id,
                uploaded_at=None,
                processed_at=datetime.utcnow(),
            )
        )
        self._db.commit()

    @staticmethod
    def empty_release_stats() -> dict[str, int]:
        return {
            "total": 0,
            "added": 0,
            "updated": 0,
            "new": 0,
            "skipped": 0,
            "waiting_master": 0,
            "unchanged": 0,
            "comments": 0,
        }

    @classmethod
    def merge_release_stats(cls, acc: dict[str, int], part: dict[str, int]) -> dict[str, int]:
        for key in cls.empty_release_stats():
            acc[key] = acc.get(key, 0) + int(part.get(key, 0) or 0)
        return acc

    @staticmethod
    def format_batch_summary(prefix: str, batch: dict[str, int], *, releases: int) -> str:
        return (
            f"{prefix}: релизов={releases}, "
            f"добавлено={batch.get('added', 0)}, обновлено={batch.get('updated', 0)}, "
            f"пропущено={batch.get('skipped', 0)}, waiting_master={batch.get('waiting_master', 0)}"
            + (
                f", comments={batch['comments']}"
                if batch.get("comments")
                else ""
            )
        )

    async def process_release(
        self,
        release_id: int,
        release_alias: str | None = None,
        *,
        list_updated_at: str | None = None,
        list_fresh_at: str | None = None,
        refresh_qb_meta: bool = False,
    ) -> dict[str, int]:
        torrents_payload = await self._al_client.get_torrents_for_release(
            release_id,
            include=[
                "id",
                "description",
                "codec",
                "type",
                "quality",
                "label",
                "info_hash",
                "hash",
                "size",
                "updated_at",
            ],
        )
        torrents = self._iter_torrents(torrents_payload)
        if not torrents:
            self._add_log(f"Релиз {release_id}: торренты не найдены", "debug")
            mark_release_processed(
                self._db,
                release_id,
                updated_at=list_updated_at,
                fresh_at=list_fresh_at,
                torrents_fingerprint_value="",
            )
            return self.empty_release_stats()

        fingerprint = torrents_fingerprint(torrents)
        all_seen = True
        for torrent in torrents:
            torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            if torrent_id is None:
                all_seen = False
                break
            raw_hash = torrent.get("info_hash") or torrent.get("hash")
            info_hash = self._normalize_api_info_hash(raw_hash)
            exists = self._db.scalar(self.build_seen_exists_query(torrent_id, info_hash).limit(1))
            if exists is None:
                all_seen = False
                break

        if all_seen:
            comments_updated = 0
            if refresh_qb_meta:
                comments_updated = self._refresh_qb_comments(
                    release_id=release_id,
                    release_alias=release_alias,
                    torrents=torrents,
                )
            unchanged = 1 if should_skip_by_torrents_fingerprint(self._db, release_id, fingerprint) else 0
            if unchanged and not refresh_qb_meta:
                self._add_log(
                    f"Релиз {release_id}: без изменений (fingerprint торрентов), "
                    "пропуск get_release/скачивания",
                    "debug",
                )
            elif unchanged and refresh_qb_meta:
                self._add_log(
                    f"Релиз {release_id}: fingerprint без изменений, "
                    f"обновлены comments={comments_updated}",
                    "debug",
                )
            else:
                self._add_log(
                    f"Релиз {release_id}: все торренты уже в seen, обновлён checkpoint без get_release"
                    + (f", comments={comments_updated}" if refresh_qb_meta else ""),
                    "debug",
                )
            mark_release_processed(
                self._db,
                release_id,
                updated_at=list_updated_at,
                fresh_at=list_fresh_at,
                torrents_fingerprint_value=fingerprint,
            )
            return {
                "total": len(torrents),
                "added": 0,
                "updated": comments_updated,
                "new": comments_updated,
                "skipped": len(torrents),
                "waiting_master": 0,
                "unchanged": unchanged,
                "comments": comments_updated,
            }

        release_payload = await self._al_client.get_release(
            release_id,
            include=["id", "alias", "name", "season", "year", "description", "updated_at", "fresh_at"],
        )
        if not isinstance(release_payload, dict):
            raise RuntimeError(f"Релиз {release_id}: AniLibria API вернул некорректные метаданные")

        api_updated_at, api_fresh_at = extract_release_markers(release_payload)
        if list_updated_at and not api_updated_at:
            api_updated_at = list_updated_at
        if list_fresh_at and not api_fresh_at:
            api_fresh_at = list_fresh_at

        passkey = await ensure_passkey_stored(self._db)
        if passkey:
            self._al_client.passkey = passkey
        elif not self._al_client.passkey:
            self._add_log(
                "Passkey AniLibria не найден: войдите в API в настройках, иначе трекер без ?pk=",
                "warning",
            )

        qb = self._try_connect_master()
        archive_service = TorrentArchiveService(self._db)
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        category = archive_service._build_category(release_payload)
        meta_clients = self._qb_clients_for_meta() if refresh_qb_meta else []

        stats = self.empty_release_stats()
        stats["total"] = len(torrents)
        errors = 0
        for torrent in torrents:
            torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            if torrent_id is None:
                self._add_log(f"Релиз {release_id}: пропуск торрента без корректного id", "warning")
                stats["skipped"] += 1
                continue

            raw_hash = torrent.get("info_hash") or torrent.get("hash")
            info_hash = self._normalize_api_info_hash(raw_hash)
            if raw_hash and info_hash is None:
                self._add_log(
                    f"Релиз {release_id}: торрент {torrent_id}: некорректный info_hash от API, "
                    "будет вычислен из файла",
                    "warning",
                )

            exists = self._db.scalar(self.build_seen_exists_query(torrent_id, info_hash).limit(1))
            if exists is not None:
                if refresh_qb_meta and meta_clients:
                    alias_value = self._alias_for_release(
                        release_id,
                        release_alias or self._clean_release_alias(release_payload),
                    )
                    release_url = build_release_torrents_url(alias_value, site_url=site_url)
                    hashes = self._candidate_hashes_for_torrent(
                        torrent_id=torrent_id,
                        api_hash=info_hash,
                        seen=exists,
                    )
                    if release_url and hashes:
                        commented = False
                        for _role, client in meta_clients:
                            for hash_for_qb in hashes:
                                if _ensure_torrent_comment(
                                    client,
                                    hash_for_qb,
                                    release_url,
                                    attempts=3,
                                    delay_sec=0.2,
                                    require_present=True,
                                ):
                                    commented = True
                                    break
                        if commented:
                            stats["updated"] += 1
                            stats["comments"] += 1
                            stats["new"] += 1
                self._add_log(f"Релиз {release_id}: торрент {torrent_id} уже обработан, пропуск", "debug")
                stats["skipped"] += 1
                continue

            alias_value = self._alias_for_release(
                release_id,
                release_alias or self._clean_release_alias(release_payload),
            )
            alias_text = f" ({alias_value})" if alias_value else ""
            display_name = build_qb_torrent_name_from_payloads(release_payload, {**torrent, "id": torrent_id})
            release_url = build_release_torrents_url(alias_value, site_url=site_url)
            if not release_url:
                self._add_log(
                    f"Торрент {torrent_id}: нет alias — comment в qB не будет установлен",
                    "warning",
                )
            pipeline = None
            try:
                torrent_bytes = await self._al_client.download_torrent_file(torrent_id)
                torrent_bytes = ensure_announce_passkey(torrent_bytes, self._al_client.passkey)
                if self._al_client.passkey and b"?pk=" not in torrent_bytes:
                    self._add_log(
                        f"Торрент {torrent_id}: не удалось встроить pk в announce",
                        "warning",
                    )
                final_hash = sanitize_info_hash(torrent_info_hash(torrent_bytes))
                pipeline = self._pipeline.create_discovered(final_hash, release_id, torrent_id)
                self._add_log(
                    f"Pipeline создан для торрента {torrent_id}: status={pipeline.status}, hash={final_hash}",
                    "debug",
                )

                archive_service.save_torrent(
                    torrent_bytes=torrent_bytes,
                    info_hash=final_hash,
                    release_id=release_id,
                    release_alias=alias_value,
                    torrent_payload={**torrent, "id": torrent_id},
                    release_payload=release_payload,
                )
                self._add_log(
                    f"Архив обновлен для торрента {torrent_id}: data/torrents/{final_hash}.torrent",
                    "debug",
                )

                if qb is None:
                    self._pipeline.mark_waiting_master(pipeline, self._master_wait_reason)
                    self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                    stats["waiting_master"] += 1
                    self._add_log(
                        f"Торрент {torrent_id} отложен (waiting_master) для релиза {release_id}{alias_text}: "
                        f"{self._master_wait_reason}",
                        "warning",
                    )
                    continue

                try:
                    added_new = qb_add_torrent(
                        qb,
                        torrent_bytes,
                        rename=display_name,
                        comment=release_url,
                        category=category,
                    )
                except Exception as add_exc:
                    if should_wait_for_qb(add_exc):
                        self._master_wait_reason = qb_client_wait_message("master", add_exc)
                        self._add_log(f"{self._master_wait_reason} (торрент {torrent_id})", "warning")
                        qb = None
                        self._pipeline.mark_waiting_master(pipeline, self._master_wait_reason)
                        self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                        stats["waiting_master"] += 1
                        continue
                    raise

                if added_new:
                    stats["added"] += 1
                    self._add_log(
                        f"Торрент {torrent_id} добавлен в master: {display_name}",
                        "debug",
                    )
                else:
                    stats["updated"] += 1
                    self._add_log(
                        f"Торрент {torrent_id} уже есть в master (Conflict), "
                        f"обновлены имя/комментарий: {display_name}",
                        "debug",
                    )

                self._pipeline.mark_master_added(pipeline)
                self._add_log(f"Торрент {torrent_id} переведен в status=master_added", "debug")
                self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                stats["new"] += 1
                self._add_log(
                    f"Обработан торрент {torrent_id} для релиза {release_id}{alias_text}",
                    "debug",
                )
            except Exception as exc:
                self._db.rollback()
                errors += 1
                if pipeline is not None:
                    try:
                        self._pipeline.mark_failed(pipeline, str(exc))
                    except Exception:
                        self._db.rollback()
                self._add_log(f"Релиз {release_id}: ошибка обработки торрента {torrent_id}: {exc}", "error")
                stats["skipped"] += 1
                continue

        if errors == 0:
            mark_release_processed(
                self._db,
                release_id,
                updated_at=api_updated_at,
                fresh_at=api_fresh_at,
                torrents_fingerprint_value=fingerprint,
            )
        else:
            # Иначе markers совпадут со старым checkpoint и early-skip скроет ретрай.
            invalidate_release_checkpoint(self._db, release_id)
        return stats
