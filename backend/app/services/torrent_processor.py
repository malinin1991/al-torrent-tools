from datetime import datetime
from typing import Any

import qbittorrentapi
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import JobLog, QbClient, SeenTorrent
from app.providers.anilibria.client import AniLibriaClient
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    ensure_announce_passkey,
    qb_add_torrent,
    qb_client_wait_message,
    sanitize_info_hash,
    should_wait_for_qb,
    torrent_info_hash,
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
        try:
            master = self._get_master_client()
            qb = qbittorrentapi.Client(
                host=master.host,
                port=master.port,
                username=master.username,
                password=master.password_encrypted,
            )
            qb.auth_log_in()
            return qb
        except Exception as exc:
            if "Не найден активный qBittorrent" in str(exc):
                self._master_wait_reason = qb_client_wait_message("master", missing=True)
                self._add_log(
                    f"{self._master_wait_reason} Торренты → waiting_master.",
                    "warning",
                )
                return None
            if should_wait_for_qb(exc):
                self._master_wait_reason = qb_client_wait_message("master", exc)
                self._add_log(
                    f"{self._master_wait_reason} Торренты → waiting_master.",
                    "warning",
                )
                return None
            raise

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

    async def process_release(self, release_id: int, release_alias: str | None = None) -> dict[str, int]:
        torrents_payload = await self._al_client.get_torrents_for_release(
            release_id,
            include=["id", "description", "codec", "type", "quality", "label", "info_hash", "hash", "size"],
        )
        torrents = self._iter_torrents(torrents_payload)
        if not torrents:
            self._add_log(f"Релиз {release_id}: торренты не найдены")
            return {"total": 0, "new": 0, "skipped": 0, "waiting_master": 0}
        release_payload = await self._al_client.get_release(
            release_id,
            include=["id", "alias", "name", "season", "year", "description"],
        )
        if not isinstance(release_payload, dict):
            raise RuntimeError(f"Релиз {release_id}: AniLibria API вернул некорректные метаданные")

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

        stats = {"total": len(torrents), "new": 0, "skipped": 0, "waiting_master": 0}
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
                self._add_log(f"Релиз {release_id}: торрент {torrent_id} уже обработан, пропуск", "debug")
                stats["skipped"] += 1
                continue

            alias_value = release_alias or self._clean_release_alias(release_payload)
            alias_text = f" ({alias_value})" if alias_value else ""
            display_name = build_qb_torrent_name_from_payloads(release_payload, {**torrent, "id": torrent_id})
            release_url = build_release_torrents_url(alias_value, site_url=site_url)
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
                self._add_log(f"Pipeline создан для торрента {torrent_id}: status={pipeline.status}, hash={final_hash}")

                archive_service.save_torrent(
                    torrent_bytes=torrent_bytes,
                    info_hash=final_hash,
                    release_id=release_id,
                    release_alias=alias_value,
                    torrent_payload={**torrent, "id": torrent_id},
                    release_payload=release_payload,
                )
                self._add_log(f"Архив обновлен для торрента {torrent_id}: data/torrents/{final_hash}.torrent")

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
                    self._add_log(f"Торрент {torrent_id} добавлен в master: {display_name}")
                else:
                    self._add_log(
                        f"Торрент {torrent_id} уже есть в master (Conflict), обновлены имя/комментарий: {display_name}"
                    )

                self._pipeline.mark_master_added(pipeline)
                self._add_log(f"Торрент {torrent_id} переведен в status=master_added")
                self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                stats["new"] += 1
                self._add_log(f"Обработан торрент {torrent_id} для релиза {release_id}{alias_text}")
            except Exception as exc:
                self._db.rollback()
                if pipeline is not None:
                    try:
                        self._pipeline.mark_failed(pipeline, str(exc))
                    except Exception:
                        self._db.rollback()
                self._add_log(f"Релиз {release_id}: ошибка обработки торрента {torrent_id}: {exc}", "error")
                stats["skipped"] += 1
                continue
        return stats
