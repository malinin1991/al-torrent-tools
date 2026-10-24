import asyncio

import httpx
import pytest

from app.providers.anilibria.client import AniLibriaClient, AniLibriaNotFoundError


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


def test_only_primary_404_confirms_not_found(monkeypatch):
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


def test_torrent_absence_is_confirmed_by_primary_404_without_fallback(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "fallback.test":
            pytest.fail("Confirmed primary 404 must not call fallback")
        return httpx.Response(404)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    client = AniLibriaClient("https://primary.test", "https://fallback.test", request_retries=1)
    assert asyncio.run(client.get_torrent(1)) is None
    assert asyncio.run(client.torrent_exists(1)) is False
    assert calls == ["primary.test", "primary.test"]


@pytest.fixture
def mock_client(monkeypatch):
    original = httpx.AsyncClient

    def build(handler, *, request_retries=1):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
        return AniLibriaClient(
            "https://primary.test/api/v1", "https://fallback.test/api/v1",
            request_retries=request_retries, retry_delay_ms=0,
        )

    return build


def request_for(client, kind):
    if kind == "json":
        return client.get_release(1)
    if kind == "torrent":
        return client.download_torrent_file(1)
    return client.auth_login("user", "password")


@pytest.mark.parametrize("kind", ["json", "torrent", "login"])
def test_primary_success_never_calls_fallback(mock_client, kind):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        assert request.url.host == "primary.test"
        if kind == "torrent":
            return httpx.Response(200, content=b"torrent-file")
        return httpx.Response(200, json={"id": 1, "token": "session-token"})

    client = mock_client(handler)
    assert asyncio.run(request_for(client, kind))
    assert calls == ["primary.test"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@pytest.mark.parametrize("kind", ["json", "torrent", "login"])
def test_primary_terminal_http_error_never_calls_fallback(mock_client, status, kind):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        assert request.url.host == "primary.test"
        return httpx.Response(status)

    client = mock_client(handler, request_retries=3)
    expected = AniLibriaNotFoundError if status == 404 and kind == "json" else RuntimeError
    with pytest.raises(expected) as exc:
        asyncio.run(request_for(client, kind))
    assert type(exc.value) is expected
    assert calls == ["primary.test"]


@pytest.mark.parametrize("body", [
    b"{ 'data': 'queue accepted, OK' }",
    b'{"data": "queue accepted, OK"}',
])
@pytest.mark.parametrize("kind", ["json", "torrent", "login"])
def test_queue_stub_is_an_error_and_can_use_fallback(mock_client, body, kind):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(200, content=body, headers={"Content-Type": "application/octet-stream"})
        if kind == "torrent":
            return httpx.Response(200, content=b"torrent-file")
        return httpx.Response(200, json={"id": 1, "token": "session-token"})

    client = mock_client(handler)
    result = asyncio.run(request_for(client, kind))
    expected = {"id": 1, "token": "session-token", "_source": "site"} if kind == "json" else (
        b"torrent-file" if kind == "torrent" else "session-token"
    )
    assert result == expected
    assert calls == ["primary.test", "fallback.test"]


@pytest.mark.parametrize("body", [
    b"{ 'data': 'queue accepted, OK' }",
    b'{"data": "queue accepted, OK"}',
])
@pytest.mark.parametrize("kind", ["json", "torrent", "login"])
def test_queue_stub_from_all_hosts_never_becomes_success_or_absence(mock_client, body, kind):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, content=body)

    client = mock_client(handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(request_for(client, kind))
    assert type(exc.value) is RuntimeError
    if kind == "json":
        assert "ответ-заглушка queue accepted" in str(exc.value)
    assert calls == ["primary.test", "fallback.test"]


@pytest.mark.parametrize("primary_failure", ["timeout", "invalid_json", "queue", "503"])
def test_fallback_404_after_primary_failure_is_not_absence(mock_client, primary_failure):
    def handler(request):
        if request.url.host == "fallback.test":
            return httpx.Response(404)
        if primary_failure == "timeout":
            raise httpx.ReadTimeout("secret-token", request=request)
        if primary_failure == "invalid_json":
            return httpx.Response(200, content=b"upstream unavailable")
        if primary_failure == "queue":
            return httpx.Response(200, json={"data": "queue accepted, OK"})
        return httpx.Response(503)

    client = mock_client(handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client.torrent_exists(1))
    assert type(exc.value) is RuntimeError
    assert "secret-token" not in str(exc.value)


@pytest.mark.parametrize("primary_failure", ["timeout", "429", "503", "invalid_json"])
def test_json_fallback_still_recovers_primary_failure(mock_client, primary_failure):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "fallback.test":
            return httpx.Response(200, json={"id": 1})
        if primary_failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if primary_failure == "invalid_json":
            return httpx.Response(200, content=b"upstream unavailable")
        return httpx.Response(int(primary_failure))

    client = mock_client(handler)
    assert asyncio.run(client.get_release(1)) == {"id": 1, "_source": "site"}
    assert calls == ["primary.test", "fallback.test"]
