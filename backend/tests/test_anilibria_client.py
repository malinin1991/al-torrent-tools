import asyncio

import httpx
import pytest

from app.providers.anilibria.client import AniLibriaClient


def test_http_error_reports_status_without_url_or_response_secrets(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        422, request=request, json={"errors": {"limit": ["limit must not exceed 50"]}, "secret": "response-secret"}
    ))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    client = AniLibriaClient("https://public.test/api/v1?secret=url-secret", request_retries=1)
    with pytest.raises(RuntimeError, match="HTTP 422") as exc:
        asyncio.run(client.catalog_releases(page=1, limit=100))
    assert "url-secret" not in str(exc.value)
    assert "response-secret" not in str(exc.value)


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


def test_not_found_requires_all_hosts_to_return_404(monkeypatch):
    import pytest
    from app.providers.anilibria.client import AniLibriaNotFoundError

    original = httpx.AsyncClient
    for primary_status, expected in [(404, AniLibriaNotFoundError), (503, RuntimeError), (401, RuntimeError)]:
        def handler(request):
            status = primary_status if request.url.host == "primary.test" else 404
            return httpx.Response(status, request=request)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
        client = AniLibriaClient(
            base_url="https://primary.test", fallback_base_url="https://fallback.test",
            request_retries=1, retry_delay_ms=0,
        )
        with pytest.raises(expected) as exc:
            asyncio.run(client.get_release(1))
        assert type(exc.value) is expected


@pytest.mark.parametrize("limit, expected", [(0, 1), (1, 1), (50, 50), (51, 50), (100, 50)])
def test_catalog_limit_respects_public_api_bounds(monkeypatch, limit, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    client = AniLibriaClient("https://public.test/api/v1")
    asyncio.run(client.catalog_releases(limit=limit))
    assert requests[0].url.params["limit"] == str(expected)


@pytest.mark.parametrize("primary_status", [401, 503, 200])
def test_torrent_absence_is_not_confirmed_by_fallback_404_alone(monkeypatch, primary_status):
    def handler(request):
        if request.url.host == "primary.test":
            # HTTP 200 with invalid JSON is also an indeterminate result.
            return httpx.Response(primary_status, content=b"unavailable")
        return httpx.Response(404)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    client = AniLibriaClient("https://primary.test", "https://fallback.test", request_retries=1)
    with pytest.raises(RuntimeError):
        asyncio.run(client.torrent_exists(1))


def test_torrent_absence_is_confirmed_when_all_hosts_return_404(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    client = AniLibriaClient("https://primary.test", "https://fallback.test", request_retries=1)
    assert asyncio.run(client.get_torrent(1)) is None
    assert asyncio.run(client.torrent_exists(1)) is False
