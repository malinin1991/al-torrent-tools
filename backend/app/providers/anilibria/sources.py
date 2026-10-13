"""Единый выбор site → подходящий backoffice, без fallback при сетевых ошибках."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Release, TorrentArchive
from app.providers.anilibria.backoffice import BackofficeClient, resource_id
from app.providers.anilibria.client import AniLibriaClient, AniLibriaNotFoundError


def eligible(payload: dict) -> bool:
    from app.services.torrent_qb_meta import parse_api_bool
    return any(parse_api_bool(payload.get(k)) is True for k in ("is_hidden", "is_blocked_by_copyrights"))


class ReleaseSourceClient:
    def __init__(self, public: AniLibriaClient, backoffice: BackofficeClient, db=None):
        self.public = public
        self.backoffice = backoffice
        self.db = db
        self._releases: dict[int, dict] = {}
        self._torrents: dict[int, list[dict]] = {}

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
        try:
            # Поля статуса и карточка нужны даже при sparse include / неизменном hash.
            payload = await self.public.get_release(release_id_or_alias)
        except AniLibriaNotFoundError:
            payload = None
        if payload is not None:
            if not isinstance(payload, dict) or type(payload.get("id")) is not int:
                raise RuntimeError("Публичный API: некорректная карточка релиза")
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
        if not eligible(payload):
            raise AniLibriaNotFoundError("Релиз не доступен на сайте и не скрыт/заблокирован в админке")
        payload = dict(payload)
        if self.db is not None:
            from app.services.runtime_settings import get_setting_value
            if not get_setting_value(self.db, "backoffice_baseline_completed_at", ""):
                archives = self.db.scalars(select(TorrentArchive).where(TorrentArchive.release_id == payload["id"])).all()
                if not archives or any(a.notification_baseline for a in archives):
                    payload["_notification_baseline"] = True
        return self.remember(payload)

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
