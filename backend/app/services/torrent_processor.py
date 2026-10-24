from app.utils.datetime_fmt import utcnow
from typing import Any
from contextlib import contextmanager

import qbittorrentapi
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import ExtraUrl, JobLog, QbClient, SeenTorrent, TorrentArchive
from app.providers.anilibria.client import AniLibriaClient
from app.services.anilibria_auth import ensure_passkey_stored
from app.services.sync_outcomes import current_outcome
from app.services.pipeline import TorrentPipelineService
from app.services.qbittorrent import (
    _ensure_torrent_comment,
    _ensure_torrent_tags,
    collect_client_info_hashes,
    ensure_announce_passkey,
    ensure_torrent_rename,
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
from app.services.hevc_pairing import sync_hevc_pair_events_for_release
from app.services.telegram_notify import enqueue_pipeline_telegram_notification
from app.services.torrent_archive import TorrentArchiveService
from app.services.release_source import release_url as source_release_url
from app.services.file_tracker import update_api_present_for_release
from app.services.torrent_qb_meta import (
    build_qb_torrent_name_from_archive,
    build_qb_torrent_name_from_payloads,
    build_release_torrents_url,
    extract_release_block_flags,
    extract_release_genres,
    extract_release_members,
    genres_from_quality_json,
    members_from_quality_json,
    resolve_anilibria_site_url,
)


class TorrentProcessor:
    # Поля торрента для archive save / meta (как telegram handler + created_at).
    RELEASE_TORRENTS_INCLUDE = [
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
        "created_at",
    ]

    def __init__(self, db: Session, job_id: int, client: AniLibriaClient) -> None:
        self._db = db
        self._job_id = job_id
        self._al_client = client
        self._pipeline = TorrentPipelineService(db, job_id=job_id)
        self._master_wait_reason = qb_client_wait_message("master", missing=True)
        self._retry_targets = None
        self._retry_qb_role = None
        self._policy_targets = None
        self._retry_operation = None
        self._active_release_id = None
        self._metadata_waiting_roles = set()

    def _source(self, release_id: int) -> str:
        from app.providers.anilibria.sources import ReleaseSourceClient
        if isinstance(self._al_client, ReleaseSourceClient):
            return self._al_client._releases.get(release_id, {}).get("_source", "unknown")
        return "site"

    def _failure(self, release_id: int, exc, *, torrent_id=None, info_hash=None,
                 operation="process", qb_role=None, force_qb_load=False):
        outcome = current_outcome(self._db, self._job_id)
        if outcome is not None:
            from app.providers.anilibria.sources import ReleaseSourceClient
            payload = self._al_client._releases.get(release_id, {}) if isinstance(self._al_client, ReleaseSourceClient) else {}
            outcome.failure(release_id=release_id, torrent_id=torrent_id, info_hash=info_hash,
                source=self._source(release_id), operation=operation, qb_role=qb_role, message=exc,
                retry_params={"force_qb_load": force_qb_load, "notification_baseline": bool(payload.get("_notification_baseline"))})

    def _torrent_outcome(self, release_id, torrent_id, info_hash, status):
        outcome = current_outcome(self._db, self._job_id)
        if outcome is not None:
            outcome.torrent(release_id, torrent_id, info_hash, status)

    def _has_release_errors(self, release_id):
        outcome = current_outcome(self._db, self._job_id)
        return outcome is not None and any(f["release_id"] == release_id for f in outcome.failures.values())

    def _metadata_failed(self, release_id, torrent_id, info_hash, role, client, operation, desired=None):
        """Verify final state: legacy helpers return False for both no-op and error."""
        if current_outcome(self._db, self._job_id) is None:
            return
        try:
            items = list(client.torrents_info(torrent_hashes=info_hash) or [])
            present = bool(items)
            if present and desired is not None:
                if operation == "comment":
                    props = client.torrents_properties(torrent_hash=info_hash)
                    current = props.get("comment") if isinstance(props, dict) else getattr(props, "comment", None)
                    if isinstance(current, str) and current.strip() == str(desired).strip():
                        return
                elif operation == "rename":
                    current = items[0].get("name") if isinstance(items[0], dict) else getattr(items[0], "name", None)
                    if isinstance(current, str) and current.strip() == str(desired).strip():
                        return
                elif operation == "tags":
                    raw = items[0].get("tags") if isinstance(items[0], dict) else getattr(items[0], "tags", None)
                    current = {t.strip().casefold() for t in (raw or "").split(",")} if isinstance(raw, str) else set()
                    wanted = {t.casefold() for t in desired}
                    from app.services.torrent_policy import SOURCE_TAGS
                    managed = self._managed_qb_genres(info_hash, desired)
                    extra = (current - wanted - SOURCE_TAGS) if managed is None else (current & {t.casefold() for t in managed} - wanted - SOURCE_TAGS)
                    if wanted.issubset(current) and not extra:
                        return
        except Exception as exc:
            if should_wait_for_qb(exc):
                self._torrent_outcome(release_id, torrent_id, info_hash, "waiting")
                return
            self._failure(release_id, exc, torrent_id=torrent_id, info_hash=info_hash, operation=operation, qb_role=role)
            return
        if present:
            self._failure(release_id, "Метаданные не применены к существующей раздаче", torrent_id=torrent_id,
                info_hash=info_hash, operation=operation, qb_role=role)

    def _check_stop(self):
        outcome = current_outcome(self._db, self._job_id)
        if outcome is not None:
            from app.services.job_runner import JobStopRequested, is_stop_requested
            if is_stop_requested(self._db, self._job_id):
                raise JobStopRequested()

    def _add_log(self, message: str, level: str = "info") -> None:
        from app.logging_filters import redact_secrets
        message = redact_secrets(message)
        self._db.add(JobLog(job_id=self._job_id, message=message, level=level))
        self._db.commit()

    def _sync_hevc_pair_events(self, release_id: int) -> None:
        """Переходы «нет HEVC»↔пара на pipeline_events (без спама на каждом прогоне)."""
        try:
            n = sync_hevc_pair_events_for_release(
                self._db, release_id, job_id=self._job_id
            )
            if n:
                self._add_log(
                    f"Релиз {release_id}: hevc_status events={n}",
                    "debug",
                )
        except Exception as exc:
            from app.services.job_runner import JobStopRequested
            if isinstance(exc, JobStopRequested):
                raise
            self._failure(release_id, exc)
            self._add_log(
                f"Релиз {release_id}: hevc_status sync failed: {exc}",
                "warning",
            )

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
                self._metadata_waiting_roles.add(role_key)
                self._master_wait_reason = qb_client_wait_message("master", exc)
                self._add_log(
                    f"{self._master_wait_reason} Торренты → waiting_master.",
                    "warning",
                )
                return None
            if role_key == "master" and "Не найден" in str(exc):
                return None
            if role_key != "master" and should_wait_for_qb(exc):
                self._metadata_waiting_roles.add(role_key)
                self._add_log(f"{qb_client_wait_message(role_key, exc)} (meta refresh)", "warning")
                return None
            if role_key == "master":
                raise
            if self._active_release_id is not None:
                self._failure(self._active_release_id, exc, operation="meta", qb_role=role_key)
            self._add_log(f"qB {role_key}: {exc} (meta refresh)", "warning")
            return None

    def _qb_clients_for_meta(self) -> list[tuple[str, qbittorrentapi.Client]]:
        clients: list[tuple[str, qbittorrentapi.Client]] = []
        self._metadata_waiting_roles = set()
        roles = (self._retry_qb_role,) if self._retry_qb_role else ("master", "slave")
        for role in roles:
            client = self._try_connect_qb(role, log_missing=False)
            if client is not None:
                clients.append((role, client))
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
        if self._retry_targets is not None:
            return [api_hash] if api_hash else []
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

    def _genres_from_archives(self, release_id: int) -> list[str]:
        rows = self._db.scalars(
            select(TorrentArchive).where(TorrentArchive.release_id == release_id)
        ).all()
        for row in rows:
            genres = genres_from_quality_json(
                row.quality_json if isinstance(row.quality_json, dict) else None
            )
            if genres:
                return genres
        return []

    def _persist_genres_to_archives(self, release_id: int, genres: list[str]) -> None:
        rows = self._db.scalars(
            select(TorrentArchive).where(TorrentArchive.release_id == release_id)
        ).all()
        changed = False
        for row in rows:
            if self._retry_targets is not None and (row.torrent_id, row.info_hash.lower()) not in (self._policy_targets or set()):
                continue
            quality = dict(row.quality_json) if isinstance(row.quality_json, dict) else {}
            if genres_from_quality_json(quality) == genres:
                continue
            quality["genres"] = genres
            previous = quality.get("qb_managed_genres", genres_from_quality_json(row.quality_json))
            quality["qb_managed_genres"] = list(dict.fromkeys(previous + genres))
            row.quality_json = quality
            changed = True
        if changed:
            self._db.commit()

    def _persist_members_and_blocks_to_archives(
        self,
        release_id: int,
        *,
        members: list[dict[str, str]] | None = None,
        is_blocked_by_geo: bool | None = None,
        is_blocked_by_copyrights: bool | None = None,
    ) -> None:
        """Пишет members / is_blocked_* в quality_json всех архивов релиза."""
        if members is None and is_blocked_by_geo is None and is_blocked_by_copyrights is None:
            return
        rows = self._db.scalars(
            select(TorrentArchive).where(TorrentArchive.release_id == release_id)
        ).all()
        changed = False
        for row in rows:
            if self._retry_targets is not None and (row.torrent_id, row.info_hash.lower()) not in (self._policy_targets or set()):
                continue
            quality = dict(row.quality_json) if isinstance(row.quality_json, dict) else {}
            row_changed = False
            if members is not None and members_from_quality_json(quality) != members:
                quality["members"] = members
                row_changed = True
            if (
                is_blocked_by_geo is not None
                and bool(quality.get("is_blocked_by_geo")) != is_blocked_by_geo
            ):
                quality["is_blocked_by_geo"] = is_blocked_by_geo
                row_changed = True
            if (
                is_blocked_by_copyrights is not None
                and bool(quality.get("is_blocked_by_copyrights")) != is_blocked_by_copyrights
            ):
                quality["is_blocked_by_copyrights"] = is_blocked_by_copyrights
                row_changed = True
            if row_changed:
                row.quality_json = quality
                changed = True
        if changed:
            self._db.commit()

    def _persist_release_ui_meta_from_payload(
        self, release_id: int, release_payload: dict[str, Any]
    ) -> None:
        """Жанры + members + блокировки → releases (+ dual-write в quality_json архивов)."""
        from app.services.release_meta import upsert_release_meta

        upsert_release_meta(self._db, release_id, release_payload, commit=True)

        genres = extract_release_genres(release_payload)
        if genres or (release_payload.get("_source") == "backoffice" and isinstance(release_payload.get("genres"), list)):
            self._persist_genres_to_archives(release_id, genres)
        members = extract_release_members(release_payload)
        geo, copy = extract_release_block_flags(release_payload)
        # Пишем каждый флаг только если ключ есть в payload (sparse include=).
        self._persist_members_and_blocks_to_archives(
            release_id,
            members=members if members or "members" in release_payload else None,
            is_blocked_by_geo=(
                geo if "is_blocked_by_geo" in release_payload else None
            ),
            is_blocked_by_copyrights=(
                copy if "is_blocked_by_copyrights" in release_payload else None
            ),
        )

    async def _resolve_genres_for_meta(
        self,
        release_id: int,
        release_payload: dict[str, Any] | None = None,
    ) -> list[str]:
        """Жанры (+ side-effect: upsert releases/members/blocks).

        С payload — сразу persist.
        Без payload (full_sync all_seen + refresh_qb_meta) — всегда get_release:
        иначе жанры из quality_json коротнули бы путь и состав/блокировки
        никогда не попали бы в таблицы ``releases`` / ``release_members``.
        """
        if release_payload is not None:
            self._persist_release_ui_meta_from_payload(release_id, release_payload)
            genres = extract_release_genres(release_payload)
            if genres or (release_payload.get("_source") == "backoffice" and isinstance(release_payload.get("genres"), list)):
                return genres
        try:
            payload = await self._al_client.get_release(
                release_id,
                include=[
                    "id",
                    "alias",
                    "name",
                    "genres",
                    "members",
                    "is_blocked_by_geo",
                    "is_blocked_by_copyrights",
                ],
            )
        except Exception as exc:
            from app.services.job_runner import JobStopRequested
            if isinstance(exc, JobStopRequested):
                raise
            self._failure(release_id, exc, operation="meta")
            self._add_log(f"Релиз {release_id}: не удалось получить genres: {exc}", "warning")
            return self._genres_from_archives(release_id)
        if not isinstance(payload, dict):
            return self._genres_from_archives(release_id)
        self._persist_release_ui_meta_from_payload(release_id, payload)
        genres = extract_release_genres(payload)
        if genres or (payload.get("_source") == "backoffice" and isinstance(payload.get("genres"), list)):
            return genres
        return self._genres_from_archives(release_id)

    def _managed_qb_genres(self, info_hash: str, desired: list[str]) -> list[str] | None:
        from app.providers.anilibria.sources import ReleaseSourceClient
        if not isinstance(self._al_client, ReleaseSourceClient):
            return None
        archive = self._db.scalar(select(TorrentArchive).where(TorrentArchive.info_hash == info_hash).limit(1))
        quality = archive.quality_json if archive is not None and isinstance(archive.quality_json, dict) else {}
        known = quality.get("qb_managed_genres", quality.get("genres", []))
        return list(dict.fromkeys((known if isinstance(known, list) else []) + desired))

    def _refresh_qb_tags(
        self,
        *,
        release_id: int,
        torrents: list[dict[str, Any]],
        genre_tags: list[str],
        clients: list | None = None,
    ) -> int:
        """Проставляет genre tags на master/slave для уже известных торрентов."""
        if not genre_tags:
            from app.providers.anilibria.sources import ReleaseSourceClient
            if not isinstance(self._al_client, ReleaseSourceClient):
                return 0
        clients = self._qb_clients_for_meta() if clients is None else clients
        if not clients:
            for t in torrents:
                self._torrent_outcome(release_id, self._to_int(t.get("id") or t.get("torrent_id")), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "waiting")
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
            for _role, client in clients:
                for info_hash in hashes:
                    if _ensure_torrent_tags(
                        client,
                        info_hash,
                        genre_tags,
                        attempts=3,
                        delay_sec=0.2,
                        require_present=True,
                        managed_tags=self._managed_qb_genres(info_hash, genre_tags),
                    ):
                        updated += 1
                        break
                else:
                    self._metadata_failed(release_id, torrent_id, hashes[0], _role, client, "tags", genre_tags)
        if updated:
            self._add_log(
                f"Релиз {release_id}: обновлено tags на qB: {updated} ({genre_tags})",
                "debug",
            )

        return updated

    def _refresh_qb_comments(
        self,
        *,
        release_id: int,
        release_alias: str | None,
        torrents: list[dict[str, Any]],
        clients: list | None = None,
    ) -> int:
        """Проставляет URL релиза в comment на master/slave для уже известных торрентов."""
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        alias = self._alias_for_release(release_id, release_alias)
        release_url = source_release_url(self._db, release_id, alias, site_url=site_url)
        if not release_url:
            self._add_log(
                f"Релиз {release_id}: нет alias — нечего писать в comment",
                "warning",
            )
            return 0

        clients = self._qb_clients_for_meta() if clients is None else clients
        if not clients:
            for t in torrents:
                self._torrent_outcome(release_id, self._to_int(t.get("id") or t.get("torrent_id")), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "waiting")
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
                    self._metadata_failed(release_id, torrent_id, hashes[0], role, client, "comment", release_url)
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

    def _archive_hash_mismatches(self, torrent: dict[str, Any]) -> bool:
        """True если активный архив есть, но info_hash ≠ API (нужен download, не meta-only)."""
        torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
        if torrent_id is None:
            return False
        raw_hash = torrent.get("info_hash") or torrent.get("hash")
        api_hash = self._normalize_api_info_hash(raw_hash)
        if not api_hash:
            return False
        archive = TorrentArchiveService(self._db)._find_active_archive(torrent_id=torrent_id)
        if archive is None:
            return False
        archived = (archive.info_hash or "").strip().lower()
        return bool(archived and archived != api_hash)

    def _display_name_for_torrent(
        self,
        torrent: dict[str, Any],
        *,
        release_payload: dict[str, Any] | None,
    ) -> str | None:
        torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
        archive = None
        if torrent_id is not None:
            archive = TorrentArchiveService(self._db)._find_active_archive(torrent_id=torrent_id)
        if archive is not None:
            name = build_qb_torrent_name_from_archive(
                anime_name=archive.anime_name,
                torrent_description=archive.torrent_description,
                torrent_type=archive.torrent_type,
                quality_json=archive.quality_json if isinstance(archive.quality_json, dict) else None,
            )
            return name or None
        if isinstance(release_payload, dict) and release_payload:
            return build_qb_torrent_name_from_payloads(
                release_payload,
                {**torrent, "id": torrent_id} if torrent_id is not None else torrent,
            )
        return None

    def _refresh_qb_renames(
        self,
        *,
        release_id: int,
        torrents: list[dict[str, Any]],
        release_payload: dict[str, Any] | None = None,
        clients: list | None = None,
    ) -> int:
        """Переименовывает торренты на master/slave по актуальному display name."""
        clients = self._qb_clients_for_meta() if clients is None else clients
        if not clients:
            for t in torrents:
                self._torrent_outcome(release_id, self._to_int(t.get("id") or t.get("torrent_id")), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "waiting")
            return 0

        updated = 0
        for torrent in torrents:
            display_name = self._display_name_for_torrent(torrent, release_payload=release_payload)
            if not display_name:
                continue
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
            for _role, client in clients:
                for info_hash in hashes:
                    if ensure_torrent_rename(
                        client,
                        info_hash,
                        display_name,
                        require_present=True,
                    ):
                        self._metadata_failed(release_id, torrent_id, info_hash, _role, client, "rename", display_name)
                        updated += 1
                        break
                else:
                    self._metadata_failed(release_id, torrent_id, hashes[0], _role, client, "rename", display_name)
        if updated:
            self._add_log(
                f"Релиз {release_id}: обновлено rename на qB: {updated}",
                "debug",
            )
        return updated

    async def refresh_release_meta(
        self,
        release_id: int,
        release_alias: str | None = None,
        torrents: list[dict[str, Any]] | None = None,
        *,
        torrent_id_filter: int | None = None,
        release_payload: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        with self._release_source_update(release_id):
            return await self._refresh_release_meta(
                release_id, release_alias, torrents,
                torrent_id_filter=torrent_id_filter, release_payload=release_payload,
            )

    async def _refresh_release_meta(
        self,
        release_id: int,
        release_alias: str | None = None,
        torrents: list[dict[str, Any]] | None = None,
        *,
        torrent_id_filter: int | None = None,
        release_payload: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Общее ядро meta-refresh: архив → rename/comment/tags (без clear seen, без TG).

        Используется из ``process_release(refresh_qb_meta=True)`` и джобов meta_sync.
        """
        if torrents is None:
            torrents_payload = await self._al_client.get_torrents_for_release(
                release_id,
                include=list(self.RELEASE_TORRENTS_INCLUDE),
            )
            from app.jobs.ongoing import _validated_items
            torrents = _validated_items(torrents_payload)

        if torrent_id_filter is not None:
            torrents = [
                t
                for t in torrents
                if self._to_int(t.get("id") or t.get("torrent_id")) == torrent_id_filter
            ]

        handoff = 0
        archive_updated = 0
        payload = release_payload
        if payload is None:
            try:
                fetched = await self._al_client.get_release(
                    release_id,
                    include=[
                        "id",
                        "alias",
                        "name",
                        "season",
                        "year",
                        "description",
                        "genres",
                        "members",
                        "is_blocked_by_geo",
                        "is_blocked_by_copyrights",
                    ],
                )
                if isinstance(fetched, dict):
                    payload = fetched
            except Exception as exc:
                from app.services.job_runner import JobStopRequested
                if isinstance(exc, JobStopRequested):
                    raise
                self._failure(release_id, exc, operation="meta")
                self._add_log(
                    f"Релиз {release_id}: не удалось получить release для meta: {exc}",
                    "warning",
                )

        genre_tags = await self._resolve_genres_for_meta(release_id, payload)

        archive_service = TorrentArchiveService(self._db)
        meta_torrents: list[dict[str, Any]] = []
        for torrent in torrents:
            if self._archive_hash_mismatches(torrent):
                handoff += 1
                continue
            status = archive_service.update_archive_meta_from_api_payload(
                release_id=release_id,
                torrent_payload=torrent,
                release_payload=payload,
                release_alias=release_alias
                or (self._clean_release_alias(payload) if isinstance(payload, dict) else None),
            )
            if status == "hash_mismatch":
                handoff += 1
                continue
            if status == "updated":
                archive_updated += 1
            if status in {"updated", "noop", "missing"}:
                # missing: всё равно пробуем rename/comment/tags по API hash / seen
                meta_torrents.append(torrent)

        if not genre_tags:
            self._add_log(
                f"Релиз {release_id}: genres не найдены — tags на qB не обновятся",
                "debug",
            )

        clients = self._qb_clients_for_meta()
        if self._metadata_waiting_roles:
            for t in meta_torrents:
                self._torrent_outcome(release_id, self._to_int(t.get("id") or t.get("torrent_id")), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "waiting")
        renames_updated = self._refresh_qb_renames(
            release_id=release_id,
            torrents=meta_torrents,
            clients=clients,
            release_payload=payload,
        ) if self._retry_operation in {None, "process", "meta", "rename"} else 0
        comments_updated = self._refresh_qb_comments(
            release_id=release_id,
            release_alias=release_alias
            or (self._clean_release_alias(payload) if isinstance(payload, dict) else None),
            torrents=meta_torrents,
            clients=clients,
        ) if self._retry_operation in {None, "process", "meta", "comment"} else 0
        tags_updated = self._refresh_qb_tags(
            release_id=release_id,
            torrents=meta_torrents,
            clients=clients,
            genre_tags=genre_tags,
        ) if self._retry_operation in {None, "process", "meta", "tags"} else 0
        return {
            "archive": archive_updated,
            "renames": renames_updated,
            "comments": comments_updated,
            "tags": tags_updated,
            "handoff": handoff,
        }

    def backfill_qb_comments_from_archive(self) -> dict[str, int]:
        """Массово проставляет URL + genre tags из torrent_archive на master/slave (после full_sync)."""
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        clients = self._qb_clients_for_meta()
        if not clients:
            self._add_log("Backfill meta: нет доступных qB", "warning")
            return {"archives": 0, "updated": 0, "missing": 0, "tags_updated": 0}

        present_by_role: dict[str, set[str] | None] = {}
        for role, client in clients:
            try:
                present_by_role[role] = collect_client_info_hashes(client)
                self._add_log(
                    f"Backfill meta: на {role} торрентов с hash={len(present_by_role[role] or [])}",
                    "debug",
                )
            except Exception as exc:
                present_by_role[role] = None
                self._add_log(
                    f"Backfill meta: не удалось получить список с {role}: {exc} — "
                    "будем пробовать по одному",
                    "warning",
                )

        archives = self._db.scalars(select(TorrentArchive)).all()
        updated = 0
        tags_updated = 0
        missing = 0
        for archive in archives:
            try:
                info_hash = sanitize_info_hash(archive.info_hash)
            except ValueError:
                continue
            release_url = source_release_url(self._db, archive.release_id, archive.release_alias, site_url=site_url)
            genre_tags = genres_from_quality_json(
                archive.quality_json if isinstance(archive.quality_json, dict) else None
            )
            if not release_url and not genre_tags:
                continue

            for role, client in clients:
                present = present_by_role.get(role)
                if present is not None and info_hash not in present:
                    missing += 1
                    continue
                require_present = present is None
                if release_url and _ensure_torrent_comment(
                    client,
                    info_hash,
                    release_url,
                    attempts=2,
                    delay_sec=0.15,
                    require_present=require_present,
                ):
                    updated += 1
                elif release_url:
                    missing += 1
                    self._metadata_failed(archive.release_id, archive.torrent_id, info_hash, role, client, "comment", release_url)
                if genre_tags and _ensure_torrent_tags(
                    client,
                    info_hash,
                    genre_tags,
                    attempts=2,
                    delay_sec=0.15,
                    require_present=require_present,
                    managed_tags=self._managed_qb_genres(info_hash, genre_tags),
                ):
                    tags_updated += 1
                elif genre_tags:
                    self._metadata_failed(archive.release_id, archive.torrent_id, info_hash, role, client, "tags", genre_tags)

        self._add_log(
            f"Backfill meta: готово, архивов={len(archives)}, "
            f"comments={updated}, tags={tags_updated}, пропущено/нет в qB={missing}"
        )
        return {
            "archives": len(archives),
            "updated": updated,
            "missing": missing,
            "tags_updated": tags_updated,
        }
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
                processed_at=utcnow(),
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
            "tags": 0,
            "renames": 0,
            "errors": 0,
        }

    @classmethod
    def merge_release_stats(cls, acc: dict[str, int], part: dict[str, int]) -> dict[str, int]:
        for key in cls.empty_release_stats():
            acc[key] = acc.get(key, 0) + int(part.get(key, 0) or 0)
        return acc

    @staticmethod
    def format_batch_summary(prefix: str, batch: dict[str, int], *, releases: int) -> str:
        """Сводка: added/updated/skipped — торренты; comments/tags/renames — qB meta (по клиентам)."""
        return (
            f"{prefix}: релизов={releases}, "
            f"добавлено={batch.get('added', 0)}, обновлено={batch.get('updated', 0)}, "
            f"пропущено={batch.get('skipped', 0)}, waiting_master={batch.get('waiting_master', 0)}"
            + (f", comments={batch['comments']}" if batch.get("comments") else "")
            + (f", tags={batch['tags']}" if batch.get("tags") else "")
            + (f", renames={batch['renames']}" if batch.get("renames") else "")
            + (f", ошибок={batch['errors']}" if batch.get("errors") else "")
        )

    async def refresh_api_present_only(self, release_id: int) -> dict[str, int]:
        """Обновить api_present по списку торрентов из API без скачивания."""
        torrents_payload = await self._al_client.get_torrents_for_release(release_id, include=["id"])
        torrents = self._iter_torrents(torrents_payload)
        present_ids: set[int] = set()
        for torrent in torrents:
            tid = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            if tid is not None:
                present_ids.add(tid)
        stats = update_api_present_for_release(self._db, release_id, present_ids)
        self._add_log(
            f"Релиз {release_id}: api_present refresh true={stats['true']}, false={stats['false']}",
            "debug",
        )
        # api_present меняет результат find_unpaired_avc — обновим hevc_status.
        self._sync_hevc_pair_events(release_id)
        return stats

    async def _force_load_release_torrents(
        self,
        release_id: int,
        release_alias: str | None,
        torrents: list[dict[str, Any]],
        *,
        release_payload: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Принудительно добавляет .torrent на master и slave (архив, иначе download).

        Не трогает pipeline/seen — только ensure present в qB (Conflict → meta update).
        """
        clients = self._qb_clients_for_meta()
        if not clients:
            self._add_log(
                f"Релиз {release_id}: force_qb_load — нет доступных qB (master/slave)",
                "warning",
            )
            for t in torrents:
                self._torrent_outcome(release_id, self._to_int(t.get("id")), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "waiting")
            return {"added": 0, "updated": 0, "skipped": 0, "waiting_master": len(torrents), "errors": 0}

        passkey = await ensure_passkey_stored(self._db)
        if passkey:
            self._al_client.passkey = passkey
        effective_passkey = self._al_client.passkey

        archive_service = TorrentArchiveService(self._db)
        site_url = resolve_anilibria_site_url(self._al_client.base_url)
        payload = release_payload
        if payload is None:
            try:
                fetched = await self._al_client.get_release(
                    release_id,
                    include=[
                        "id",
                        "alias",
                        "name",
                        "season",
                        "year",
                        "description",
                        "genres",
                    ],
                )
                if isinstance(fetched, dict):
                    payload = fetched
            except Exception as exc:
                from app.services.job_runner import JobStopRequested
                if isinstance(exc, JobStopRequested):
                    raise
                self._failure(release_id, exc, operation="meta", force_qb_load=True)
                self._add_log(
                    f"Релиз {release_id}: force_qb_load — get_release не удался: {exc}",
                    "warning",
                )

        genre_tags = extract_release_genres(payload) if isinstance(payload, dict) else []
        category = (
            archive_service._build_category(payload) if isinstance(payload, dict) else None
        )

        added = 0
        updated = 0
        skipped = 0
        errors = 0
        waiting = 0
        for torrent in torrents:
            self._check_stop()
            torrent_id = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            if torrent_id is None:
                skipped += 1
                continue

            raw_hash = torrent.get("info_hash") or torrent.get("hash")
            api_hash = self._normalize_api_info_hash(raw_hash)
            archive = archive_service._find_active_archive(
                torrent_id=torrent_id,
                info_hash=api_hash,
            )

            torrent_bytes: bytes | None = None
            if archive is not None:
                path = archive_service.resolve_file_path(archive)
                if path.exists():
                    torrent_bytes = ensure_announce_passkey(
                        path.read_bytes(),
                        effective_passkey,
                    )
                    from app.services.torrent_policy import prepare_archive_bytes, replace_trackers
                    if isinstance(payload, dict) and payload.get("_source") == "backoffice":
                        torrent_bytes = replace_trackers(torrent_bytes, effective_passkey)
                    else:
                        torrent_bytes = prepare_archive_bytes(archive, torrent_bytes, effective_passkey)
                if not category and archive.category:
                    category = archive.category
                if not genre_tags:
                    genre_tags = genres_from_quality_json(
                        archive.quality_json if isinstance(archive.quality_json, dict) else None
                    )

            if torrent_bytes is None:
                try:
                    torrent_bytes = await self._al_client.download_torrent_file(torrent_id)
                    torrent_bytes = ensure_announce_passkey(torrent_bytes, effective_passkey)
                except Exception as exc:
                    from app.services.job_runner import JobStopRequested
                    if isinstance(exc, JobStopRequested):
                        raise
                    self._add_log(
                        f"Релиз {release_id}: force_qb_load торрент {torrent_id}: "
                        f"нет файла в архиве и download failed: {exc}",
                        "error",
                    )
                    errors += 1
                    self._failure(release_id, exc, torrent_id=torrent_id, info_hash=api_hash, operation="force_load", force_qb_load=True)
                    continue

            if self._retry_targets is not None and api_hash and sanitize_info_hash(torrent_info_hash(torrent_bytes)) != api_hash:
                errors += 1
                self._failure(release_id, "Скачанный файл относится к другой версии; отправка отменена", torrent_id=torrent_id,
                    info_hash=api_hash, operation="force_load", force_qb_load=True)
                continue

            display_name = self._display_name_for_torrent(
                {**torrent, "id": torrent_id},
                release_payload=payload if isinstance(payload, dict) else None,
            )
            if not display_name and isinstance(payload, dict):
                display_name = build_qb_torrent_name_from_payloads(
                    payload,
                    {**torrent, "id": torrent_id},
                )
            alias_value = self._alias_for_release(
                release_id,
                release_alias
                or (
                    self._clean_release_alias(payload)
                    if isinstance(payload, dict)
                    else None
                )
                or (archive.release_alias if archive is not None else None),
            )
            release_url = source_release_url(self._db, release_id, alias_value, site_url=site_url)
            torrent_category = category or (archive.category if archive is not None else None)

            for role, client in clients:
                try:
                    added_new, comment_ok, tags_ok = qb_add_torrent(
                        client,
                        torrent_bytes,
                        rename=display_name,
                        comment=release_url,
                        category=torrent_category,
                        tags=genre_tags,
                        managed_tags=self._managed_qb_genres(torrent_info_hash(torrent_bytes), genre_tags),
                    )
                except Exception as exc:
                    from app.services.job_runner import JobStopRequested
                    if isinstance(exc, JobStopRequested):
                        raise
                    if should_wait_for_qb(exc):
                        waiting += 1
                        self._torrent_outcome(release_id, torrent_id, api_hash, "waiting")
                        continue
                    self._add_log(
                        f"Релиз {release_id}: force_qb_load торрент {torrent_id} → {role}: {exc}",
                        "error",
                    )
                    errors += 1
                    self._failure(release_id, exc, torrent_id=torrent_id, info_hash=api_hash, operation="force_load", qb_role=role, force_qb_load=True)
                    continue
                if added_new:
                    added += 1
                    self._add_log(
                        f"force_qb_load: торрент {torrent_id} добавлен на {role}",
                        "debug",
                    )
                else:
                    updated += 1
                    self._add_log(
                        f"force_qb_load: торрент {torrent_id} уже на {role} (Conflict/meta)",
                        "debug",
                    )
                if release_url and not comment_ok:
                    self._metadata_failed(release_id, torrent_id, api_hash, role, client, "comment", release_url)
                    self._add_log(
                        f"force_qb_load: торрент {torrent_id} comment не установлен на {role}",
                        "warning",
                    )
                if genre_tags and not tags_ok:
                    self._metadata_failed(release_id, torrent_id, api_hash, role, client, "tags", genre_tags)
                    self._add_log(
                        f"force_qb_load: торрент {torrent_id} tags не установлены на {role}",
                        "warning",
                    )

        self._add_log(
            f"Релиз {release_id}: force_qb_load готово — "
            f"added={added}, updated={updated}, skipped={skipped}",
            "info",
        )
        return {"added": added, "updated": updated, "skipped": skipped, "errors": errors, "waiting_master": waiting}

    @contextmanager
    def _release_source_update(self, release_id: int):
        """Shared release lock and source policy for discovery and scoped metadata."""
        from app.services.release_lock import release_lock
        with release_lock(self._db, release_id):
            previous_release_id = self._active_release_id
            self._active_release_id = release_id
            try:
                yield
                from app.services.release_source import apply_release_policy, release_is_offsite, tags_for_release
                if tags_for_release(self._db, release_id) or release_is_offsite(self._db, release_id):
                    options = {}
                    if self._retry_targets is not None:
                        options["torrent_targets"] = self._policy_targets or set()
                    if current_outcome(self._db, self._job_id) is not None:
                        options["on_error"] = lambda archive, role, exc: (
                            self._torrent_outcome(release_id, archive.torrent_id, archive.info_hash, "waiting")
                            if should_wait_for_qb(exc) else self._failure(
                                release_id, exc, torrent_id=archive.torrent_id, info_hash=archive.info_hash,
                                operation="source_policy", qb_role=role))
                    apply_release_policy(self._db, release_id, self._qb_clients_for_meta(),
                                         self._al_client.passkey, log=self._add_log, **options)
            finally:
                self._active_release_id = previous_release_id

    async def process_release(self, release_id: int, release_alias: str | None = None, **kwargs) -> dict[str, int]:
        targets = kwargs.pop("retry_targets", None)
        if targets is not None and any(t.get("torrent_id") is not None and self._normalize_api_info_hash(t.get("info_hash")) is None for t in targets):
            raise ValueError("Точечный повтор торрента требует точного info_hash")
        role = kwargs.pop("retry_qb_role", None)
        operation = kwargs.pop("retry_operation", None)
        previous = self._retry_targets, self._retry_qb_role, self._policy_targets, self._retry_operation
        self._retry_targets, self._retry_qb_role, self._policy_targets = targets, role, None
        self._retry_operation = operation
        try:
            with self._release_source_update(release_id):
                result = await self._process_release(release_id, release_alias, **kwargs)
            if self._has_release_errors(release_id):
                outcome = current_outcome(self._db, self._job_id)
                result["errors"] = max(int(result.get("errors", 0)), len({(f["torrent_id"], f["info_hash"]) for f in outcome.failures.values() if f["release_id"] == release_id}))
                if targets is None:
                    invalidate_release_checkpoint(self._db, release_id)
            return result
        finally:
            self._retry_targets, self._retry_qb_role, self._policy_targets, self._retry_operation = previous

    async def _process_release(
        self,
        release_id: int,
        release_alias: str | None = None,
        *,
        list_updated_at: str | None = None,
        list_fresh_at: str | None = None,
        refresh_qb_meta: bool = False,
        force_qb_load: bool = False,
        prefetched_torrents: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        if prefetched_torrents is None:
            torrents_payload = await self._al_client.get_torrents_for_release(
                release_id,
                include=list(self.RELEASE_TORRENTS_INCLUDE),
            )
            from app.jobs.ongoing import _validated_items
            torrents = _validated_items(torrents_payload)
        else:
            torrents = prefetched_torrents
        if self._retry_targets is not None:
            # Never infer presence from a stale standalone torrent card. The full
            # selected-source list is fetched under the same release lock.
            payload = await self._al_client.get_release(release_id)
            source = payload.get("_source", "site")
            from app.jobs.ongoing import _validated_items
            if prefetched_torrents is not None:
                raise ValueError("Scoped retry must fetch a fresh complete source list")
            torrents = _validated_items(torrents_payload)
            selected = []
            matched_targets = []
            outcome = current_outcome(self._db, self._job_id)
            for target in self._retry_targets:
                expected = target.get("info_hash")
                candidates = [t for t in torrents if target.get("torrent_id") is None or self._to_int(t.get("id")) == target["torrent_id"]]
                matched = [t for t in candidates if not expected or self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")) == expected]
                if target.get("source") not in {"unknown", source} or not matched:
                    if outcome:
                        outcome.stale(release_id, target.get("torrent_id"), "Версия или источник изменились; повтор пропущен")
                    continue
                selected.extend(matched)
                matched_targets.append(target)
            torrents = list({self._to_int(t["id"]): t for t in selected}.values())
            self._policy_targets = {(self._to_int(t["id"]), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash"))) for t in torrents}
            if any(t.get("notification_baseline") for t in matched_targets):
                payload["_notification_baseline"] = True
            if not torrents:
                return self.empty_release_stats()
            if self._retry_operation == "source_policy":
                for t in torrents:
                    self._torrent_outcome(release_id, self._to_int(t["id"]), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "processed")
                return {**self.empty_release_stats(), "total": len(torrents)}
            if self._retry_operation == "force_load":
                force = await self._force_load_release_torrents(release_id, release_alias, torrents, release_payload=payload)
                for t in torrents:
                    self._torrent_outcome(release_id, self._to_int(t["id"]), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "processed")
                return {**self.empty_release_stats(), "total": len(torrents), **force,
                        "new": int(force.get("added", 0)) + int(force.get("updated", 0))}
            if self._retry_operation in {"meta", "comment", "tags", "rename"}:
                meta = await self._refresh_release_meta(release_id, release_alias, torrents, release_payload=payload)
                for t in torrents:
                    self._torrent_outcome(release_id, self._to_int(t["id"]), self._normalize_api_info_hash(t.get("info_hash") or t.get("hash")), "processed")
                return {**self.empty_release_stats(), "total": len(torrents),
                        "comments": meta.get("comments", 0), "tags": meta.get("tags", 0), "renames": meta.get("renames", 0)}
        if not torrents:
            if self._retry_targets is not None:
                return self.empty_release_stats()
            self._add_log(f"Релиз {release_id}: торренты не найдены", "debug")
            update_api_present_for_release(self._db, release_id, set())
            mark_release_processed(
                self._db,
                release_id,
                updated_at=list_updated_at,
                fresh_at=list_fresh_at,
                torrents_fingerprint_value="",
            )
            self._sync_hevc_pair_events(release_id)
            return self.empty_release_stats()

        fingerprint = torrents_fingerprint(torrents)
        present_ids: set[int] = set()
        for torrent in torrents:
            tid = self._to_int(torrent.get("id") or torrent.get("torrent_id"))
            if tid is not None:
                present_ids.add(tid)
        if self._retry_targets is None:
            update_api_present_for_release(self._db, release_id, present_ids)
        # Backfill api_created_at из list (в т.ч. already-seen / full_sync), не перекачивая .torrent.
        filled = TorrentArchiveService(self._db).fill_missing_api_created_at(
            release_id, torrents
        ) if self._retry_targets is None else 0
        if filled:
            self._add_log(
                f"Релиз {release_id}: api_created_at backfill={filled}",
                "debug",
            )

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
            # Seen, но hash сменился → нужен download/supersede, не meta-only.
            if self._archive_hash_mismatches(torrent):
                all_seen = False
                break

        if all_seen:
            comments_updated = 0
            tags_updated = 0
            renames_updated = 0
            if refresh_qb_meta:
                # Внешняя process_release уже держит lock и применит source policy.
                meta = await self._refresh_release_meta(
                    release_id,
                    release_alias,
                    torrents,
                )
                comments_updated = int(meta.get("comments", 0) or 0)
                tags_updated = int(meta.get("tags", 0) or 0)
                renames_updated = int(meta.get("renames", 0) or 0)
            force_added = 0
            force_updated = 0
            if force_qb_load:
                force = await self._force_load_release_torrents(
                    release_id,
                    release_alias,
                    torrents,
                )
                force_added = int(force.get("added", 0) or 0)
                force_updated = int(force.get("updated", 0) or 0)
            # updated/new — только торренты (force_qb / Conflict), не сумма meta-ops.
            unchanged = 1 if should_skip_by_torrents_fingerprint(self._db, release_id, fingerprint) else 0
            if unchanged and not refresh_qb_meta and not force_qb_load:
                self._add_log(
                    f"Релиз {release_id}: без изменений (fingerprint торрентов), "
                    "пропуск get_release/скачивания",
                    "debug",
                )
            elif unchanged and (refresh_qb_meta or force_qb_load):
                self._add_log(
                    f"Релиз {release_id}: fingerprint без изменений, "
                    f"обновлены comments={comments_updated}, tags={tags_updated}, "
                    f"renames={renames_updated}"
                    + (f", force_qb added={force_added}/updated={force_updated}" if force_qb_load else ""),
                    "debug",
                )
            else:
                self._add_log(
                    f"Релиз {release_id}: все торренты уже в seen, обновлён checkpoint без get_release"
                    + (
                        f", comments={comments_updated}, tags={tags_updated}, "
                        f"renames={renames_updated}"
                        if refresh_qb_meta
                        else ""
                    )
                    + (
                        f", force_qb added={force_added}/updated={force_updated}"
                        if force_qb_load
                        else ""
                    ),
                    "debug",
                )
            if self._retry_targets is None and not self._has_release_errors(release_id):
                mark_release_processed(
                    self._db, release_id, updated_at=list_updated_at,
                    fresh_at=list_fresh_at, torrents_fingerprint_value=fingerprint,
                )
            elif self._has_release_errors(release_id):
                invalidate_release_checkpoint(self._db, release_id)
            for torrent in torrents:
                self._torrent_outcome(release_id, self._to_int(torrent.get("id")), self._normalize_api_info_hash(torrent.get("info_hash") or torrent.get("hash")), "processed" if force_qb_load or refresh_qb_meta else "skipped")
            self._sync_hevc_pair_events(release_id)
            return {
                "total": len(torrents),
                "added": force_added,
                "updated": force_updated,
                "new": force_added + force_updated,
                "skipped": 0 if force_qb_load else len(torrents),
                "waiting_master": int(force.get("waiting_master", 0)) if force_qb_load else 0,
                "unchanged": unchanged,
                "comments": comments_updated,
                "tags": tags_updated,
                "renames": renames_updated,
                "errors": int(force.get("errors", 0)) if force_qb_load else 0,
            }

        release_payload = await self._al_client.get_release(
            release_id,
            include=[
                "id",
                "alias",
                "name",
                "season",
                "year",
                "description",
                "genres",
                "members",
                "is_blocked_by_geo",
                "is_blocked_by_copyrights",
                "updated_at",
                "fresh_at",
            ],
        )
        if not isinstance(release_payload, dict):
            raise RuntimeError(f"Релиз {release_id}: AniLibria API вернул некорректные метаданные")
        if release_payload.get("_source") == "backoffice" and "genres" not in release_payload:
            genres = self._genres_from_archives(release_id)
            if genres:
                release_payload = {**release_payload, "genres": genres}

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
        genre_tags = extract_release_genres(release_payload)
        self._persist_release_ui_meta_from_payload(release_id, release_payload)

        stats = self.empty_release_stats()
        stats["total"] = len(torrents)
        errors = 0
        for torrent in torrents:
            self._check_stop()
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
            hash_mismatch = self._archive_hash_mismatches(torrent)
            if exists is not None and not hash_mismatch:
                if refresh_qb_meta:
                    meta = await self._refresh_release_meta(
                        release_id, release_alias, [{**torrent, "id": torrent_id}],
                        release_payload=release_payload,
                    )
                    for key in ("comments", "tags", "renames"):
                        stats[key] += int(meta.get(key, 0) or 0)
                if force_qb_load:
                    force = await self._force_load_release_torrents(
                        release_id,
                        release_alias,
                        [{**torrent, "id": torrent_id}],
                        release_payload=release_payload,
                    )
                    stats["added"] += int(force.get("added", 0) or 0)
                    stats["updated"] += int(force.get("updated", 0) or 0)
                    stats["new"] += int(force.get("added", 0) or 0) + int(
                        force.get("updated", 0) or 0
                    )
                    stats["errors"] += int(force.get("errors", 0) or 0)
                    stats["waiting_master"] += int(force.get("waiting_master", 0) or 0)
                    self._torrent_outcome(release_id, torrent_id, info_hash, "processed")
                    continue
                self._add_log(f"Релиз {release_id}: торрент {torrent_id} уже обработан, пропуск", "debug")
                stats["skipped"] += 1
                self._torrent_outcome(release_id, torrent_id, info_hash, "skipped")
                continue

            alias_value = self._alias_for_release(
                release_id,
                release_alias or self._clean_release_alias(release_payload),
            )
            alias_text = f" ({alias_value})" if alias_value else ""
            display_name = build_qb_torrent_name_from_payloads(release_payload, {**torrent, "id": torrent_id})
            release_url = source_release_url(self._db, release_id, alias_value, site_url=site_url)
            if not release_url:
                self._add_log(
                    f"Торрент {torrent_id}: нет alias — comment в qB не будет установлен",
                    "warning",
                )
            pipeline = None
            final_hash = info_hash
            try:
                torrent_bytes = await self._al_client.download_torrent_file(torrent_id)
                torrent_bytes = ensure_announce_passkey(torrent_bytes, self._al_client.passkey)
                if self._al_client.passkey and b"?pk=" not in torrent_bytes:
                    self._add_log(
                        f"Торрент {torrent_id}: не удалось встроить pk в announce",
                        "warning",
                    )
                final_hash = sanitize_info_hash(torrent_info_hash(torrent_bytes))
                if self._retry_targets is not None and info_hash and final_hash != info_hash:
                    raise RuntimeError("Скачанный файл относится к другой версии; обработка отменена")
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

                torrent_payload = {**torrent, "id": torrent_id}

                if qb is None:
                    self._pipeline.mark_waiting_master(pipeline, self._master_wait_reason)
                    enqueue_pipeline_telegram_notification(
                        self._db,
                        pipeline,
                        release_payload=release_payload,
                        torrent_payload=torrent_payload,
                    )
                    self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                    stats["waiting_master"] += 1
                    self._torrent_outcome(release_id, torrent_id, final_hash, "waiting")
                    self._add_log(
                        f"Торрент {torrent_id} отложен (waiting_master) для релиза {release_id}{alias_text}: "
                        f"{self._master_wait_reason}",
                        "warning",
                    )
                    continue

                try:
                    added_new, comment_ok, tags_ok = qb_add_torrent(
                        qb,
                        torrent_bytes,
                        rename=display_name,
                        comment=release_url,
                        category=category,
                        tags=genre_tags,
                        managed_tags=self._managed_qb_genres(final_hash, genre_tags),
                    )
                except Exception as add_exc:
                    if should_wait_for_qb(add_exc):
                        self._master_wait_reason = qb_client_wait_message("master", add_exc)
                        self._add_log(f"{self._master_wait_reason} (торрент {torrent_id})", "warning")
                        qb = None
                        self._pipeline.mark_waiting_master(pipeline, self._master_wait_reason)
                        enqueue_pipeline_telegram_notification(
                            self._db,
                            pipeline,
                            release_payload=release_payload,
                            torrent_payload=torrent_payload,
                        )
                        self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                        stats["waiting_master"] += 1
                        self._torrent_outcome(release_id, torrent_id, final_hash, "waiting")
                        continue
                    raise

                if release_url and not comment_ok:
                    self._metadata_failed(release_id, torrent_id, final_hash, "master", qb, "comment", release_url)
                    self._add_log(
                        f"Торрент {torrent_id}: comment не установлен на master",
                        "warning",
                    )
                if genre_tags and not tags_ok:
                    self._metadata_failed(release_id, torrent_id, final_hash, "master", qb, "tags", genre_tags)
                    self._add_log(
                        f"Торрент {torrent_id}: tags не установлены на master ({genre_tags})",
                        "warning",
                    )

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

                self._pipeline.mark_master_added(
                    pipeline,
                    details={
                        "added_new": added_new,
                        "qb_role": "master",
                        "display_name": display_name,
                    },
                )
                enqueue_pipeline_telegram_notification(
                    self._db,
                    pipeline,
                    release_payload=release_payload,
                    torrent_payload=torrent_payload,
                )
                self._add_log(
                    f"Торрент {torrent_id} переведен в status=master_added, tg_status={pipeline.tg_status}",
                    "debug",
                )
                self._mark_seen(torrent_id=torrent_id, info_hash=final_hash, release_id=release_id)
                stats["new"] += 1
                self._torrent_outcome(release_id, torrent_id, final_hash, "processed")
                self._add_log(
                    f"Обработан торрент {torrent_id} для релиза {release_id}{alias_text}",
                    "debug",
                )
            except Exception as exc:
                from app.services.job_runner import JobStopRequested
                if isinstance(exc, JobStopRequested):
                    raise
                self._db.rollback()
                errors += 1
                if pipeline is not None:
                    try:
                        self._pipeline.mark_failed(pipeline, str(exc))
                    except Exception:
                        self._db.rollback()
                self._add_log(f"Релиз {release_id}: ошибка обработки торрента {torrent_id}: {exc}", "error")
                self._failure(release_id, exc, torrent_id=torrent_id, info_hash=final_hash)
                continue

        errors += stats.get("errors", 0)
        if self._has_release_errors(release_id):
            errors = max(1, errors)
        if errors == 0 and self._retry_targets is None:
            mark_release_processed(
                self._db,
                release_id,
                updated_at=api_updated_at,
                fresh_at=api_fresh_at,
                torrents_fingerprint_value=fingerprint,
            )
        elif errors:
            # Иначе markers совпадут со старым checkpoint и early-skip скроет ретрай.
            invalidate_release_checkpoint(self._db, release_id)
            stats["errors"] = errors
        self._sync_hevc_pair_events(release_id)
        return stats
