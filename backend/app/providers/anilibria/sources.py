"""Выбор источника; обход Nova может продолжать последний подтверждённый backoffice."""

from __future__ import annotations

from contextlib import contextmanager

import httpx
from sqlalchemy import select

from app.db.models import Release, TorrentArchive
from app.providers.anilibria.backoffice import BackofficeClient, resource_id
from app.providers.anilibria.client import AniLibriaClient, AniLibriaNotFoundError


def eligible(payload: dict) -> bool:
    """Public flags that force the backoffice source even for an HTTP 200 card."""
    from app.services.torrent_qb_meta import parse_api_bool
    return any(parse_api_bool(payload.get(k)) is True for k in ("is_hidden", "is_blocked_by_copyrights"))


def backoffice_eligible(payload: dict) -> bool:
    """Nova may also supply a geo-blocked release missing from the public API."""
    from app.services.torrent_qb_meta import parse_api_bool
    return eligible(payload) or parse_api_bool(payload.get("is_blocked_by_geo")) is True


class SourceCheckDeferred(RuntimeError):
    """Публичный API недоступен, источник пока нельзя подтвердить."""


class BackofficeReleaseSkipped(Exception):
    """Обычный site-релиз или неподходящая карточка Nova; архив не меняем."""


class ReleaseSourceClient:
    def __init__(self, public: AniLibriaClient, backoffice: BackofficeClient, db=None):
        self.public = public
        self.backoffice = backoffice
        self.db = db
        self._releases: dict[int, dict] = {}
        self._torrents: dict[int, list[dict]] = {}
        self._backoffice_watched: set[int] | None = None
        self._source_check_error: str | None = None
        self.source_check_errors: dict[int, str] = {}

    @contextmanager
    def backoffice_scan(self, watched):
        """Только для обхода Nova; get_release вызывается под release_lock processor."""
        previous = self._backoffice_watched
        previous_error = self._source_check_error
        self._backoffice_watched = set(watched)
        self._source_check_error = None
        self._releases.clear()
        self._torrents.clear()
        self.source_check_errors.clear()
        try:
            yield
        finally:
            self._backoffice_watched = previous
            self._source_check_error = previous_error
            self._releases.clear()
            self._torrents.clear()

    def _remember_backoffice(self, payload: dict) -> dict:
        payload = dict(payload)
        if self.db is not None:
            from app.services.runtime_settings import get_setting_value
            global_initial = not get_setting_value(self.db, "backoffice_baseline_completed_at", "")
            geo_initial = (backoffice_eligible(payload) and not eligible(payload)
                           and not get_setting_value(self.db, "backoffice_geo_baseline_completed_at", ""))
            if global_initial or geo_initial:
                archives = self.db.scalars(select(TorrentArchive).where(TorrentArchive.release_id == payload["id"])).all()
                if not archives or any(a.notification_baseline for a in archives):
                    payload["_notification_baseline"] = True
        return self.remember(payload)

    @staticmethod
    def _validate_public_release(payload, rid=None):
        if not isinstance(payload, dict) or type(payload.get("id")) is not int or (rid is not None and payload["id"] != rid):
            raise RuntimeError("Публичный API: некорректная карточка релиза")

    def _retain_backoffice_source(self, rid, admin, admin_error):
        self.source_check_errors[rid] = self._source_check_error
        previous = self.db.get(Release, rid, populate_existing=True) if self.db is not None else None
        if admin_error is not None and not isinstance(admin_error, AniLibriaNotFoundError):
            raise admin_error
        if previous is None or previous.source != "backoffice":
            raise SourceCheckDeferred("Проверка сайта недоступна; источник сохранён, обработка отложена")
        if admin_error is not None:
            raise SourceCheckDeferred("Карточка Nova недоступна; подтверждение источника отложено") from admin_error
        payload = {**admin, "_source": "backoffice", "_source_confirmed": False,
                   "is_hidden": previous.is_hidden,
                   "is_blocked_by_copyrights": previous.is_blocked_by_copyrights}
        return self._remember_backoffice(payload)

    async def _get_backoffice_scan_release(self, rid: int):
        # Обновляем identity map после ожидания release_lock: другая джоба
        # могла подтвердить новый источник до получения этой блокировки.
        previous = self.db.get(Release, rid, populate_existing=True) if self.db is not None else None
        watched = rid in self._backoffice_watched or (previous is not None and previous.source == "backoffice")
        # Чтение Nova не зависит от каталога/доступности публичного API.
        admin, admin_error = None, None
        try:
            admin = await self.backoffice.get_release(rid)
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            admin_error = exc
        if admin is not None and not backoffice_eligible(admin) and not watched:
            raise BackofficeReleaseSkipped()
        # Одна недоступность не должна умножать timeout/retries на сотни релизов.
        # Новый обход снова начинает с проверки публичного API.
        if self._source_check_error is not None:
            return self._retain_backoffice_source(rid, admin, admin_error)
        try:
            public = await self.public.get_release(rid)
            self._validate_public_release(public, rid)
        except AniLibriaNotFoundError:
            public = None
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            self._source_check_error = type(exc).__name__
            return self._retain_backoffice_source(rid, admin, admin_error)
        if public is not None and not eligible(public):
            payload = self.remember({**public, "_source": "site", "is_hidden": False,
                                     "is_blocked_by_copyrights": False})
            if not watched:
                raise BackofficeReleaseSkipped()
            return payload
        if admin_error is not None:
            raise admin_error
        if not backoffice_eligible(admin):
            raise BackofficeReleaseSkipped()
        return self._remember_backoffice(admin)

    def __getattr__(self, name):
        return getattr(self.public, name)

    @property
    def passkey(self):
        return self.public.passkey

    @passkey.setter
    def passkey(self, value):
        self.public.passkey = value

    def remember(self, payload: dict) -> dict:
        rid = int(payload["id"])
        self._releases[rid] = payload
        if self.db is not None:
            from app.services.release_meta import upsert_release_meta
            upsert_release_meta(self.db, rid, payload)
        return payload

    async def get_release(self, release_id_or_alias, include=None, exclude=None):
        raw = str(release_id_or_alias)
        rid = int(raw) if raw.isdigit() else None
        if rid in self._releases:
            return self._releases[rid]
        if self._backoffice_watched is not None and rid is not None:
            return await self._get_backoffice_scan_release(rid)
        try:
            # Поля статуса и карточка нужны даже при sparse include / неизменном hash.
            payload = await self.public.get_release(release_id_or_alias)
        except AniLibriaNotFoundError:
            payload = None
        if payload is not None:
            self._validate_public_release(payload, rid)
            rid = payload["id"]
            if not eligible(payload):
                return self.remember({**payload, "_source": "site", "is_hidden": False,
                                      "is_blocked_by_copyrights": False})
        if rid is None and self.db is not None:
            rid = self.db.scalar(select(Release.release_id).where(Release.release_alias == raw).limit(1))
        if rid is None:
            # Nova search — только кандидаты; alias проверяется на точное совпадение.
            resources = await self.backoffice.list_resources("anime-releases", search=raw)
            matches = []
            for resource in resources:
                item = await self.backoffice.get_release(resource_id(resource))
                if str(item.get("alias") or "").casefold() == raw.casefold():
                    matches.append(item)
            if len(matches) != 1:
                raise AniLibriaNotFoundError("Релиз не найден по точному alias")
            payload = matches[0]
        else:
            payload = await self.backoffice.get_release(rid)
        if not backoffice_eligible(payload):
            raise AniLibriaNotFoundError("Релиз не доступен на сайте и не скрыт/заблокирован в админке")
        return self._remember_backoffice(payload)

    async def get_releases_list(self, aliases=None, ids=None, include=None, exclude=None):
        if aliases is None and ids is None:
            return await self.public.get_releases_list(include=include, exclude=exclude)
        result = []
        for ref in list(ids or []) + list(aliases or []):
            try:
                result.append(await self.get_release(ref, include=include, exclude=exclude))
            except AniLibriaNotFoundError:
                pass
        return result

    async def get_torrents_for_release(self, release_id, include=None, exclude=None):
        rid = int(release_id)
        payload = await self.get_release(rid)
        if rid not in self._torrents:
            if payload["_source"] == "backoffice":
                if self._backoffice_watched is not None and self.db is not None and isinstance(self.backoffice, BackofficeClient):
                    from app.services.backoffice_torrent_cache import BackofficeTorrentCacheStore
                    cache = BackofficeTorrentCacheStore(self.db, self.backoffice.base_url)
                    torrents = await self.backoffice.get_torrents_for_release(rid, cache=cache)
                else:
                    torrents = await self.backoffice.get_torrents_for_release(rid)
            else:
                torrents = await self.public.get_torrents_for_release(rid)
                if isinstance(torrents, dict):
                    torrents = next((torrents[k] for k in ("data", "list", "items", "results") if k in torrents), None)
            if not isinstance(torrents, list) or any(not isinstance(t, dict) or type(t.get("id")) is not int for t in torrents):
                raise RuntimeError("Некорректный список торрентов")
            self._torrents[rid] = torrents
        return self._torrents[rid]

    async def get_torrent(self, hash_or_id, include=None, exclude=None):
        raw = str(hash_or_id).lower()
        tid = int(raw) if raw.isdigit() and len(raw) != 40 else None
        for items in self._torrents.values():
            for item in items:
                if (item["id"] == tid if tid is not None else str(item.get("hash") or item.get("info_hash") or "").lower() == raw):
                    return item
        rid = None
        if self.db is not None:
            archive = self.db.scalar(select(TorrentArchive).where(
                TorrentArchive.torrent_id == tid if tid is not None else TorrentArchive.info_hash == raw
            ).order_by(TorrentArchive.id.desc()).limit(1))
            if archive is not None:
                rid = archive.release_id
        if rid is None:
            item = await self.public.get_torrent(hash_or_id)
            if item is not None:
                if not isinstance(item, dict):
                    raise RuntimeError("Некорректный ответ torrent API")
                release = item.get("release")
                rid = item.get("release_id") or (release.get("id") if isinstance(release, dict) else None)
            elif tid is not None:
                try:
                    item = await self.backoffice.get_torrent(tid)
                    rid = item.get("release_id")
                except AniLibriaNotFoundError:
                    return None
            else:
                return None
        if rid is None:
            raise RuntimeError("Невозможно определить релиз торрента")
        try:
            torrents = await self.get_torrents_for_release(rid)
        except AniLibriaNotFoundError:
            return None
        return next((t for t in torrents if t["id"] == tid) if tid is not None else
                    (t for t in torrents if str(t.get("hash") or t.get("info_hash") or "").lower() == raw), None)

    async def torrent_exists(self, hash_or_id):
        return await self.get_torrent(hash_or_id) is not None

    async def pipeline_torrent_exists(self, pipeline):
        try:
            torrents = await self.get_torrents_for_release(pipeline.release_id)
        except AniLibriaNotFoundError:
            return False
        return any(t["id"] == pipeline.torrent_id and
                   str(t.get("hash") or t.get("info_hash") or "").lower() == pipeline.info_hash.lower()
                   for t in torrents)

    async def download_torrent_file(self, hash_or_id, pk=None):
        item = await self.get_torrent(hash_or_id)
        if item is None:
            raise AniLibriaNotFoundError("Торрент отсутствует в выбранном источнике")
        rid = next(rid for rid, items in self._torrents.items() if any(t["id"] == item["id"] for t in items))
        release = self._releases[rid]
        if release["_source"] == "backoffice":
            data = await self.backoffice.download_torrent_file(item["id"])
        else:
            data = await self.public.download_torrent_file(item["id"], pk=pk)
        from app.services.qbittorrent import torrent_info_hash
        expected = item.get("hash") or item.get("info_hash")
        if expected and torrent_info_hash(data) != str(expected).lower():
            raise RuntimeError("Hash скачанного .torrent изменился; требуется повторная синхронизация")
        if not isinstance(item.get("size"), int):
            from app.services.torrent_policy import _decode, raw_torrent_fields
            info, _ = _decode(raw_torrent_fields(data)[b"info"])
            files = info.get(b"files")
            size = sum(f[b"length"] for f in files) if isinstance(files, list) else info.get(b"length")
            if isinstance(size, int) and size >= 0:
                item["size"] = size
        patched = release["_source"] == "backoffice"
        if not patched and self.db is not None:
            archive = self.db.scalar(select(TorrentArchive).where(TorrentArchive.info_hash == torrent_info_hash(data)).limit(1))
            patched = archive is not None and archive.backoffice_trackers
        if patched:
            from app.services.torrent_policy import replace_trackers
            data = replace_trackers(data, pk or self.passkey)
        return data
