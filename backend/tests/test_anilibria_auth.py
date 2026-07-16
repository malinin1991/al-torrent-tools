import asyncio
import json

import httpx

from app.providers.anilibria.client import AniLibriaClient


def test_anilibria_auth_login_returns_token(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/accounts/users/auth/login")
        body = json.loads(request.content.decode())
        assert body == {"login": "user", "password": "pass"}
        return httpx.Response(200, json={"token": "session-token-123"})

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    client = AniLibriaClient(base_url="https://anilibria.test/api/v1", request_retries=1, retry_delay_ms=0)

    token = asyncio.run(client.auth_login("user", "pass"))

    assert token == "session-token-123"


def test_anilibria_auth_login_401(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    client = AniLibriaClient(base_url="https://anilibria.test/api/v1", request_retries=1, retry_delay_ms=0)

    try:
        asyncio.run(client.auth_login("user", "wrong"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "неверный логин" in str(exc).lower()
