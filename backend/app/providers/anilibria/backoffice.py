"""Read-only Nova API. POST разрешён исключительно для создания login-сессии."""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

from app.providers.anilibria.client import AniLibriaNotFoundError


class BackofficeError(RuntimeError):
    pass


MEMBERS_RESOURCE = "anime-releases-releasesMembers"


class _LoginForm(HTMLParser):
    token: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "_token":
            self.token = values.get("value")


def field(resource: dict, attribute: str, component: str | None = None) -> dict | None:
    fields = resource.get("fields")
    if not isinstance(fields, list):
        raise BackofficeError("Backoffice: resource.fields должен быть списком")
    return next((f for f in fields if isinstance(f, dict)
                 and f.get("attribute") == attribute
                 and (component is None or f.get("component") == component)), None)


def _raw_resource_id(resource: dict):
    raw = resource.get("id")
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None:
        raw = (field(resource, "id") or {}).get("value")
    return raw


def resource_id(resource: dict) -> int:
    raw = _raw_resource_id(resource)
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise BackofficeError("Backoffice: ресурс без числового id") from None
    if value <= 0:
        raise BackofficeError("Backoffice: некорректный id")
    return value


def member_id(resource: dict) -> str:
    try:
        return str(UUID(str(_raw_resource_id(resource))))
    except (ValueError, TypeError, AttributeError):
        raise BackofficeError("Backoffice: участник без корректного UUID") from None


def normalize_genres(item: dict) -> list[dict]:
    """Nova multiselect: выбранные ID + options с названиями жанров."""
    values = item.get("value")
    if values is None or values == []:
        return []
    if not isinstance(values, list) or not isinstance(item.get("options"), list):
        raise BackofficeError("Backoffice: некорректные жанры")
    options = {str(o["value"]): o.get("label") for o in item["options"]
               if isinstance(o, dict) and "value" in o}
    result, seen = [], set()
    for value in values:
        if type(value) not in (int, str):
            raise BackofficeError("Backoffice: некорректный ID жанра")
        key = str(value)
        label = options.get(key)
        if not isinstance(label, str) or not label.strip():
            raise BackofficeError("Backoffice: нет названия выбранного жанра")
        if key not in seen:
            seen.add(key)
            result.append({"id": value, "name": label.strip()})
    return result


def normalize_member(resource: dict) -> dict:
    # ComputedField содержит отображаемый ник, включая fallback на связанного user.
    candidates = [field(resource, "ComputedField", "text-field"), field(resource, "nickname", "text-field")]
    if any(item and item.get("value") is not None and not isinstance(item["value"], str) for item in candidates):
        raise BackofficeError("Backoffice: некорректный никнейм участника")
    nickname = next((item for item in candidates if item and isinstance(item.get("value"), str) and item["value"].strip()), None)
    role = field(resource, "role", "select-field")
    if role is None or not isinstance(role.get("value"), str) or not role["value"].strip():
        raise BackofficeError("Backoffice: участник без роли")
    value = role["value"].strip()
    label = role.get("displayedAs")
    if not isinstance(label, str) or not label.strip():
        label = next((o.get("label") for o in role.get("options", [])
                      if isinstance(o, dict) and o.get("value") == value), None)
    # Старые записи могут не иметь ни псевдонима, ни user. Общий extractor
    # пропустит пустой ник при отображении, не блокируя импорт всего релиза.
    return {"id": member_id(resource), "nickname": nickname["value"].strip() if nickname else "",
            "role": {"value": value, "description": label.strip() if isinstance(label, str) and label.strip() else value}}


def normalize_release(resource: dict) -> dict[str, Any]:
    result: dict[str, Any] = {"id": resource_id(resource), "_source": "backoffice"}
    names = {}
    for attribute, name in (("name", "main"), ("name_english", "english"), ("name_alternative", "alternative")):
        item = field(resource, attribute)
        if item is not None and isinstance(item.get("value"), str):
            names[name] = item["value"]
    if names:
        result["name"] = names
    for key in ("alias", "year", "description", "updated_at", "created_at", "is_ongoing", "is_in_production"):
        item = field(resource, key)
        if item is not None:
            result[key] = item.get("value")
    for key in ("is_hidden", "is_blocked_by_copyrights", "is_blocked_by_geo"):
        item = field(resource, key)
        if item is not None:
            value = item.get("value")
            # В Nova geo — multiselect стран, а не boolean, как в публичном API.
            if key == "is_blocked_by_geo" and item.get("component") == "multiselect-field":
                if value is None:
                    result[key] = False
                elif isinstance(value, list) and all(isinstance(country, str) and country.strip() for country in value):
                    result[key] = bool(value)
                else:
                    raise BackofficeError("Backoffice: некорректный список стран is_blocked_by_geo")
                continue
            if value not in (True, False, 0, 1, "0", "1", "true", "false"):
                raise BackofficeError(f"Backoffice: некорректный {key}")
            result[key] = value in (True, 1, "1", "true")
    genres = field(resource, "genres", "multiselect-field")
    if genres is not None:
        result["genres"] = normalize_genres(genres)
    # members — отдельный GET связи; отсутствие поля не означает пустой состав.
    return result


def normalize_torrent(resource: dict) -> dict[str, Any]:
    result: dict[str, Any] = {"id": resource_id(resource)}
    release = field(resource, "release", "belongs-to-field")
    if release and release.get("belongsToId") is not None:
        result["release_id"] = int(release["belongsToId"])
    for key in ("description", "hash", "created_at", "updated_at", "bitrate", "is_hardsub", "seeders", "leechers"):
        item = field(resource, key)
        if item is not None:
            result[key] = item.get("value")
    info_hash = str(result.get("hash") or "").lower().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", info_hash):
        raise BackofficeError("Backoffice: торрент без корректного v1 hash")
    result["hash"] = info_hash
    for key in ("type", "quality", "codec", "color"):
        item = field(resource, key, "select-field")
        if item is not None and item.get("value") is not None:
            value = str(item["value"])
            # Публичный API сохраняет x264/AVC в value, но отдаёт короткий label.
            label = {"x264/avc": "AVC", "x265/hevc": "HEVC"}.get(value.strip().casefold(), value) if key == "codec" else value
            result[key] = {"value": value, "label": label, "description": label}
    download = field(resource, "ComputedField", "file-field")
    if download:
        result["label"] = download.get("label") or download.get("value")
    return result


class BackofficeClient:
    def __init__(self, url: str, login: str, password: str, *, retries: int = 3,
                 retry_delay_sec: float = .75, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("Некорректный URL админки")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._login = login
        self._password = password
        self._cookies = httpx.Cookies()
        self._authenticated = False
        self._retries = max(1, retries)
        self._delay = retry_delay_sec
        self._transport = transport

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        # Нет универсального публичного request API; защита также на транспортном уровне.
        if not ((method == "POST" and path == "/login") or
                (method == "GET" and (path == "/login" or re.fullmatch(
                    r"/nova-api/(?:anime-(?:releases|torrents)(?:/\d+(?:/download/ComputedField)?)?|anime-releases-releasesMembers)", path)))):
            raise ValueError("Backoffice: разрешено только чтение и POST /login")
        for attempt in range(self._retries):
            try:
                async with httpx.AsyncClient(timeout=60, cookies=self._cookies,
                                             transport=self._transport) as client:
                    response = await client.request(method, self.base_url + path, **kwargs)
                    self._cookies.update(client.cookies)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < self._retries:
                        await asyncio.sleep(self._delay * (attempt + 1))
                        continue
                return response
            except httpx.TransportError:
                if attempt + 1 == self._retries:
                    raise BackofficeError("Backoffice: сетевая ошибка") from None
                await asyncio.sleep(self._delay * (attempt + 1))
        raise BackofficeError("Backoffice: запрос не завершён")

    async def _authenticate(self):
        if not self._login or not self._password:
            raise BackofficeError("Для админки нужны сохранённые логин и пароль AniLiberty")
        for attempt in range(2):
            form = await self._request("GET", "/login")
            if form.status_code != 200:
                raise BackofficeError(f"Backoffice: форма входа HTTP {form.status_code}")
            parser = _LoginForm()
            parser.feed(form.text)
            if not parser.token:
                raise BackofficeError("Backoffice: в форме входа отсутствует CSRF token")
            result = await self._request("POST", "/login", data={
                "_token": parser.token, "login": self._login, "password": self._password,
            }, headers={"Referer": self.base_url + "/login"})
            if result.status_code == 419 and attempt == 0:
                continue
            if result.status_code not in (200, 302, 303):
                raise BackofficeError(f"Backoffice: вход HTTP {result.status_code}")
            self._authenticated = True
            return

    async def _get(self, path: str, *, params=None, binary=False):
        for attempt in range(2):
            if not self._authenticated:
                await self._authenticate()
            response = await self._request("GET", path, params=params, headers={
                "Accept": "application/x-bittorrent" if binary else "application/json",
                "X-Requested-With": "XMLHttpRequest",
            })
            html = response.content.lstrip().startswith(b"<")
            if response.status_code in (401, 419, 302, 303) or (response.status_code == 200 and html):
                self._authenticated = False
                if attempt == 0:
                    continue
                raise BackofficeError("Backoffice: авторизация не подтверждена")
            if response.status_code == 404:
                raise AniLibriaNotFoundError("Backoffice: ресурс не найден")
            if response.status_code != 200:
                raise BackofficeError(f"Backoffice: HTTP {response.status_code}")
            if binary:
                if not response.content.startswith(b"d"):
                    raise BackofficeError("Backoffice: ответ не является .torrent")
                return response.content
            try:
                payload = response.json()
            except ValueError:
                raise BackofficeError("Backoffice: некорректный JSON") from None
            if not isinstance(payload, dict):
                raise BackofficeError("Backoffice: ожидался JSON object")
            return payload

    async def list_resources(self, resource: str, *, on_page=None, **params) -> list[dict]:
        if resource not in {"anime-releases", "anime-torrents", MEMBERS_RESOURCE}:
            raise ValueError("Неизвестный ресурс Nova")
        if resource == MEMBERS_RESOURCE and not (
            params.get("viaResource") == "anime-releases" and params.get("viaRelationship") == "members"
            and type(params.get("viaResourceId")) is int and params["viaResourceId"] > 0
        ):
            raise ValueError("Участники Nova читаются только через связь релиза")
        path = f"/nova-api/{resource}"
        result, seen = [], set()
        page = 1
        while True:
            if on_page is not None:
                on_page(page)
            payload = await self._get(path, params={**params, "perPage": 100, "page": page})
            resources = payload.get("resources")
            if not isinstance(resources, list) or "next_page_url" not in payload:
                raise BackofficeError("Backoffice: неполный ответ пагинации")
            for item in resources:
                if not isinstance(item, dict):
                    raise BackofficeError("Backoffice: некорректный ресурс")
                rid = member_id(item) if resource == MEMBERS_RESOURCE else resource_id(item)
                if rid in seen:
                    raise BackofficeError("Backoffice: нестабильная пагинация, повтор id")
                seen.add(rid)
                result.append(item)
            next_url = payload["next_page_url"]
            if next_url is None:
                total = payload.get("total")
                if isinstance(total, int) and total != len(result):
                    raise BackofficeError("Backoffice: неполный список ресурсов")
                return result
            if not resources or not isinstance(next_url, str):
                raise BackofficeError("Backoffice: некорректная следующая страница")
            parsed = urlsplit(urljoin(self.base_url, next_url))
            if f"{parsed.scheme}://{parsed.netloc}" != self.base_url or parsed.path != path:
                raise BackofficeError("Backoffice: некорректный URL пагинации")
            page += 1

    async def get_release(self, release_id: int) -> dict:
        payload = await self._get(f"/nova-api/anime-releases/{int(release_id)}")
        resource = payload.get("resource")
        if not isinstance(resource, dict):
            raise BackofficeError("Backoffice: отсутствует resource")
        result = normalize_release(resource)
        if result["id"] != int(release_id):
            raise BackofficeError("Backoffice: ID релиза в ответе не совпал")
        members = field(resource, "members", "has-many-field")
        if members is not None:
            if members.get("resourceName") != MEMBERS_RESOURCE or members.get("hasManyRelationship") != "members":
                raise BackofficeError("Backoffice: некорректная связь участников")
            resources = await self.list_resources(MEMBERS_RESOURCE, viaResource="anime-releases",
                viaResourceId=int(release_id), viaRelationship="members")
            result["members"] = [normalize_member(item) for item in resources]
        return result

    async def get_torrent(self, torrent_id: int) -> dict:
        payload = await self._get(f"/nova-api/anime-torrents/{int(torrent_id)}")
        resource = payload.get("resource")
        if not isinstance(resource, dict):
            raise BackofficeError("Backoffice: отсутствует resource")
        result = normalize_torrent(resource)
        if result["id"] != int(torrent_id):
            raise BackofficeError("Backoffice: ID торрента в ответе не совпал")
        return result

    async def get_torrents_for_release(self, release_id: int) -> list[dict]:
        resources = await self.list_resources("anime-torrents", viaResource="anime-releases",
                                             viaResourceId=int(release_id), viaRelationship="torrents")
        result = []
        for resource in resources:
            torrent = await self.get_torrent(resource_id(resource))
            if torrent.get("release_id") != int(release_id):
                raise BackofficeError("Backoffice: торрент принадлежит другому релизу")
            result.append(torrent)
        return result

    async def download_torrent_file(self, torrent_id: int) -> bytes:
        return await self._get(f"/nova-api/anime-torrents/{int(torrent_id)}/download/ComputedField", binary=True)
