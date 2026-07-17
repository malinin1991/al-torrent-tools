import asyncio
from collections.abc import Iterable
from typing import Any

import httpx


class AniLibriaClient:
    def __init__(
        self,
        base_url: str,
        fallback_base_url: str | None = None,
        bearer_token: str | None = None,
        passkey: str | None = None,
        timeout_sec: float = 20.0,
        request_retries: int = 3,
        retry_delay_ms: int = 750,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_base_url = (fallback_base_url or "").rstrip("/") or None
        self.bearer_token = bearer_token or ""
        self.passkey = (passkey or "").strip()
        self._timeout = timeout_sec
        self._request_retries = max(1, request_retries)
        self._retry_delay_sec = max(0, retry_delay_ms) / 1000

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    @staticmethod
    def _build_fields_params(include: Iterable[str] | None = None, exclude: Iterable[str] | None = None) -> dict[str, str]:
        params: dict[str, str] = {}
        if include:
            params["include"] = ",".join(include)
        if exclude:
            params["exclude"] = ",".join(exclude)
        return params

    @staticmethod
    def _should_retry_http_error(exc: httpx.HTTPError) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            return status_code == 429 or status_code >= 500
        return True

    async def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        base_urls = [self.base_url]
        if self.fallback_base_url:
            base_urls.append(self.fallback_base_url)

        last_error: Exception | None = None
        for root in base_urls:
            for attempt in range(1, self._request_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
                        response = await client.get(f"{root}{path}", params=params)
                        response.raise_for_status()
                        return response.json()
                except ValueError as exc:
                    last_error = exc
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    if not self._should_retry_http_error(exc) or attempt >= self._request_retries:
                        break
                    await asyncio.sleep(self._retry_delay_sec)
        raise RuntimeError("AniLibria API недоступен на основном и fallback URL") from last_error

    async def _request_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        base_urls = [self.base_url]
        if self.fallback_base_url:
            base_urls.append(self.fallback_base_url)

        last_error: Exception | None = None
        for root in base_urls:
            for attempt in range(1, self._request_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
                        response = await client.get(f"{root}{path}", params=params)
                        response.raise_for_status()
                        return response.content
                except httpx.HTTPError as exc:
                    last_error = exc
                    if not self._should_retry_http_error(exc) or attempt >= self._request_retries:
                        break
                    await asyncio.sleep(self._retry_delay_sec)
        raise RuntimeError("Не удалось скачать torrent-файл") from last_error

    async def _request_post_json(self, path: str, body: dict[str, Any]) -> Any:
        base_urls = [self.base_url]
        if self.fallback_base_url:
            base_urls.append(self.fallback_base_url)

        last_error: Exception | None = None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        for root in base_urls:
            for attempt in range(1, self._request_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(f"{root}{path}", json=body, headers=headers)
                        response.raise_for_status()
                        return response.json()
                except ValueError as exc:
                    last_error = exc
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 422):
                        break
                    if not self._should_retry_http_error(exc) or attempt >= self._request_retries:
                        break
                    await asyncio.sleep(self._retry_delay_sec)
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 401:
            raise RuntimeError("Не удалось авторизоваться в AniLibria API: неверный логин или пароль") from last_error
        raise RuntimeError("Не удалось выполнить вход в AniLibria API") from last_error

    async def auth_login(self, login: str, password: str) -> str:
        """POST /accounts/users/auth/login — см. AniLibria API v1."""
        payload = await self._request_post_json(
            "/accounts/users/auth/login",
            {"login": login.strip(), "password": password},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("AniLibria API вернул некорректный ответ при входе")
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("AniLibria API не вернул token при входе")
        self.bearer_token = token.strip()
        return self.bearer_token

    async def get_my_profile(self, include: Iterable[str] | None = None) -> Any:
        return await self._request_json(
            "/accounts/users/me/profile",
            self._build_fields_params(include) or None,
        )

    async def get_my_passkey(self) -> str | None:
        """passkey из профиля (torrents.passkey) — только для авторизованного пользователя."""
        if not self.bearer_token:
            return None
        profile = await self.get_my_profile(include=["torrents"])
        if not isinstance(profile, dict):
            return None
        torrents = profile.get("torrents")
        if not isinstance(torrents, dict):
            return None
        passkey = torrents.get("passkey")
        if isinstance(passkey, str) and passkey.strip():
            self.passkey = passkey.strip()
            return self.passkey
        return None

    async def ensure_passkey(self) -> str | None:
        if self.passkey:
            return self.passkey
        return await self.get_my_passkey()

    async def health(self) -> Any:
        return await self._request_json("/app/status")

    async def get_schedule_week(self, include: Iterable[str] | None = None, exclude: Iterable[str] | None = None) -> Any:
        return await self._request_json("/anime/schedule/week", self._build_fields_params(include, exclude) or None)

    async def get_release(self, release_id_or_alias: str | int, include: Iterable[str] | None = None, exclude: Iterable[str] | None = None) -> Any:
        return await self._request_json(
            f"/anime/releases/{release_id_or_alias}",
            self._build_fields_params(include, exclude) or None,
        )

    async def get_releases_list(
        self,
        aliases: Iterable[str] | None = None,
        ids: Iterable[int] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> Any:
        params = self._build_fields_params(include, exclude)
        if aliases:
            params["aliases"] = ",".join(aliases)
        if ids:
            params["ids"] = ",".join(str(item) for item in ids)
        return await self._request_json("/anime/releases/list", params or None)

    async def catalog_releases(
        self,
        page: int = 1,
        *,
        limit: int = 10,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> Any:
        params = self._build_fields_params(include, exclude)
        params["page"] = str(page)
        params["limit"] = str(max(1, limit))
        return await self._request_json("/anime/catalog/releases", params)

    async def get_torrents_for_release(
        self,
        release_id: int,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> Any:
        return await self._request_json(
            f"/anime/torrents/release/{release_id}",
            self._build_fields_params(include, exclude) or None,
        )

    async def get_torrent(
        self,
        hash_or_id: str | int,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> Any | None:
        """Данные торрента по hash/id. None если 404 (торрент удалён из API)."""
        try:
            return await self._request_json(
                f"/anime/torrents/{hash_or_id}",
                self._build_fields_params(include, exclude) or None,
            )
        except RuntimeError as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404:
                return None
            raise

    async def torrent_exists(self, hash_or_id: str | int) -> bool:
        return await self.get_torrent(hash_or_id, include=["id", "hash"]) is not None

    async def download_torrent_file(self, hash_or_id: str | int, pk: str | None = None) -> bytes:
        """Скачивает .torrent; pk подставляет passkey в announce (см. API ?pk=)."""
        passkey = (pk or self.passkey or "").strip() or None
        if passkey is None and self.bearer_token:
            passkey = await self.ensure_passkey()
        params: dict[str, Any] | None = {"pk": passkey} if passkey else None
        return await self._request_bytes(f"/anime/torrents/{hash_or_id}/file", params)
