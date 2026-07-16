import asyncio

import httpx

from app.providers.anilibria.client import AniLibriaClient


def test_anilibria_client_uses_fallback_after_primary_failure(monkeypatch) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "primary.test":
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    client = AniLibriaClient(
        base_url="https://primary.test/api/v1",
        fallback_base_url="https://fallback.test/api/v1",
        request_retries=1,
        retry_delay_ms=0,
    )

    result = asyncio.run(client.health())

    assert result == {"status": "ok"}
    assert calls == [
        "https://primary.test/api/v1/app/status",
        "https://fallback.test/api/v1/app/status",
    ]


def test_anilibria_client_retries_on_5xx(monkeypatch) -> None:
    attempts = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(502, request=request)
        return httpx.Response(200, request=request, json={"list": []})

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    client = AniLibriaClient(
        base_url="https://primary.test/api/v1",
        request_retries=3,
        retry_delay_ms=0,
    )

    result = asyncio.run(client.get_torrents_for_release(100))

    assert result == {"list": []}
    assert attempts["count"] == 3
