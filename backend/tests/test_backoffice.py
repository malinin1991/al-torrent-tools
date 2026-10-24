"""Read-only Nova, выбор источника, сохранность infohash и ручная классификация."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, Release, Setting, TorrentArchive
from app.providers.anilibria.backoffice import BackofficeClient, BackofficeError, normalize_release, normalize_torrent
from app.providers.anilibria.client import AniLibriaClient, AniLibriaNotFoundError
from app.providers.anilibria.sources import ReleaseSourceClient
from app.services.qbittorrent import torrent_info_hash
from app.services.release_meta import upsert_release_meta
from app.services.release_source import apply_release_policy
from app.services.releases_view import list_release_groups
from app.services.torrent_policy import (
    BACKOFFICE_TRACKERS, _decode, raw_torrent_fields, reconcile_source_tags,
    reconcile_trackers, replace_trackers, source_tags, tracker_urls,
)

RAW = b"d8:announce10:http://old4:infod4:name4:test6:lengthi1eee"
HASH = torrent_info_hash(RAW)


@compiles(JSONB, "sqlite")
def _jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Job(id=i, type="backoffice_sync", status="running", params_json={}) for i in (1, 2, 3)])
        session.commit()
        yield session
    engine.dispose()


def nova_resource(rid=1, **values):
    return {"id": {"value": rid}, "fields": [
        {"attribute": key, "component": "boolean-field" if key.startswith("is_") else "text-field", "value": value}
        for key, value in values.items()
    ]}


def nova_metadata_transport(*, fail_members=False, empty=False):
    """Форма ответов, проверенная на реальных карточках Nova; данные вымышлены."""
    calls = []
    def handle(request):
        assert request.method == "GET"
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/nova-api/anime-releases/1":
            resource = nova_resource(name="Title", year=2026, is_hidden=True)
            resource["fields"] += [
                {"attribute":"genres", "component":"multiselect-field", "value":None if empty else [29,"1",29],
                 "options":[{"value":1,"label":"Комедия"},{"value":29,"label":"Фэнтези"}]},
                {"attribute":"members", "component":"has-many-field", "resourceName":"anime-releases-releasesMembers",
                 "hasManyRelationship":"members", "value":None},
            ]
            return httpx.Response(200,json={"resource":resource})
        if request.url.path == "/nova-api/anime-releases-releasesMembers":
            assert request.url.params["viaResource"] == "anime-releases"
            assert request.url.params["viaResourceId"] == "1"
            assert request.url.params["viaRelationship"] == "members"
            if fail_members:
                return httpx.Response(503)
            if empty:
                return httpx.Response(200,json={"resources":[],"next_page_url":None,"total":0})
            page = int(request.url.params["page"])
            member = nova_resource(f"00000000-0000-4000-8000-{page:012d}", nickname=None)
            member["fields"] += [
                {"attribute":"ComputedField","component":"text-field","value":f"Nick {page}"},
                {"attribute":"role","component":"select-field","value":"voicing" if page==1 else "hevc",
                 "displayedAs":"Озвучка" if page==1 else "Кодирование HEVC"},
            ]
            return httpx.Response(200,json={"resources":[member],"next_page_url":
                "/nova-api/anime-releases-releasesMembers?page=2" if page==1 else None,"total":2})
        pytest.fail(f"Unexpected GET: {request.url.path}")
    client = BackofficeClient("https://admin.test", "u", "p", retries=1, transport=httpx.MockTransport(handle))
    client._authenticated = True
    return client, calls


def test_nova_reads_genre_labels_and_all_member_pages_once_per_release():
    admin, calls = nova_metadata_transport()
    public = SimpleNamespace(get_release=AsyncMock(side_effect=AniLibriaNotFoundError()))
    client = ReleaseSourceClient(public, admin)
    payload = asyncio.run(client.get_release(1))
    assert payload["genres"] == [{"id":29,"name":"Фэнтези"},{"id":"1","name":"Комедия"}]
    assert [(m["nickname"],m["role"]["value"]) for m in payload["members"]] == [("Nick 1","voicing"),("Nick 2","hevc")]
    assert payload["members"][1]["role"]["description"] == "Кодирование HEVC"
    assert asyncio.run(client.get_release(1)) is payload
    assert len(calls) == 3


@pytest.mark.parametrize("nickname", [None, "", "  "])
def test_nova_legacy_unnamed_member_does_not_block_release(nickname):
    from app.providers.anilibria.backoffice import normalize_member
    from app.services.torrent_qb_meta import extract_release_members
    resource = nova_resource("00000000-0000-4000-8000-000000000001", nickname=nickname)
    resource["fields"] += [
        {"attribute":"ComputedField","component":"text-field","value":nickname},
        {"attribute":"role","component":"select-field","value":"translating"},
        {"attribute":"user","component":"belongs-to-field","belongsToId":None},
    ]
    result = normalize_member(resource)
    assert result["nickname"] == ""
    assert extract_release_members({"members":[result]}) == []


def test_nova_metadata_refresh_updates_web_archive_and_both_qb_genre_tags(db, monkeypatch):
    from app.services.torrent_processor import TorrentProcessor
    from app.db.models import ReleaseMember
    admin, _ = nova_metadata_transport()
    public = SimpleNamespace(get_release=AsyncMock(side_effect=AniLibriaNotFoundError()), passkey="key")
    client = ReleaseSourceClient(public, admin, db)
    archive = add_archive(db)
    archive.quality_json = {"genres":["Драма"],"qb_managed_genres":["Драма"]}
    db.commit()
    master,slave = Qb(),Qb()
    for qb in (master,slave):
        qb.tags = {"Драма","manual","hidden"}
    processor = TorrentProcessor(db=db,job_id=1,client=client)
    monkeypatch.setattr(processor,"_qb_clients_for_meta",lambda:[("master",master),("slave",slave)])
    for method in ("_refresh_qb_comments","_refresh_qb_renames"):
        monkeypatch.setattr(processor,method,lambda **kwargs:0)
    for empty in (False,True):
        if empty:
            client.backoffice,_ = nova_metadata_transport(empty=True)
            client._releases.clear()
        payload = asyncio.run(client.get_release(1))
        asyncio.run(processor._refresh_release_meta(1,torrents=[{"id":10,"hash":HASH}],release_payload=payload))
        expected_genres = [] if empty else ["Фэнтези","Комедия"]
        assert db.get(Release,1).genres_json == expected_genres
        assert archive.quality_json["genres"] == expected_genres
        members = db.scalars(select(ReleaseMember).order_by(ReleaseMember.sort_order)).all()
        assert [m.role for m in members] == ([] if empty else ["voicing","hevc"])
        assert [m["nickname"] for m in archive.quality_json["members"]] == ([] if empty else ["Nick 1","Nick 2"])
        for qb in (master,slave):
            assert qb.tags == set(expected_genres) | {"manual","hidden"}


def test_nova_failed_relationship_does_not_cache_or_erase_known_metadata(db):
    upsert_release_meta(db,1,{"genres":["Драма"],"members":[{"nickname":"Old","role":{"value":"voicing"}}]})
    admin,_ = nova_metadata_transport(fail_members=True)
    public = SimpleNamespace(get_release=AsyncMock(side_effect=AniLibriaNotFoundError()))
    client = ReleaseSourceClient(public,admin,db)
    with pytest.raises(BackofficeError,match="HTTP 503"):
        asyncio.run(client.get_release(1))
    assert not client._releases
    assert db.get(Release,1).genres_json == ["Драма"]
    assert db.get(Release,1).members[0].nickname == "Old"


@pytest.mark.parametrize("values,options", [([99],[{"value":1,"label":"Комедия"}]),([1],None),([{}],[])])
def test_nova_invalid_genres_are_not_treated_as_empty(values,options):
    resource = nova_resource()
    resource["fields"].append({"attribute":"genres","component":"multiselect-field","value":values,"options":options})
    with pytest.raises(BackofficeError):
        normalize_release(resource)


def test_nova_login_cookies_html_and_expired_session():
    calls, logins, api_calls = [], [], []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(200, text="<input value='csrf' name='_token'>", headers={"set-cookie": "XSRF-TOKEN=csrf; Path=/"})
        if request.method == "POST":
            assert b"_token=csrf" in request.content
            assert "XSRF-TOKEN=csrf" in request.headers["cookie"]
            logins.append(1)
            return httpx.Response(302, headers={"set-cookie": "_al0=session; Path=/", "location": "/nova"})
        assert "_al0=session" in request.headers["cookie"]
        api_calls.append(1)
        if len(api_calls) == 1:
            return httpx.Response(200, text="<html>login</html>")
        return httpx.Response(200, json={"resource": nova_resource(name="Title", is_hidden=True, is_blocked_by_copyrights=False)})
    client = BackofficeClient("https://admin.test/nova/resources/anime-releases/{release_id}", "user", "secret", transport=httpx.MockTransport(handle))
    result = asyncio.run(client.get_release(1))
    assert result["name"] == {"main": "Title"}
    assert result["is_hidden"] is True
    assert len(logins) == 2
    assert all(method == "GET" or path == "/login" for method, path in calls)


@pytest.mark.parametrize("method,path", [("POST", "/nova-api/anime-releases"), ("DELETE", "/nova-api/anime-torrents/1"), ("GET", "/nova-api/anime-releases/action"),
    ("POST", "/nova-api/anime-releases-releasesMembers"), ("GET", "/nova-api/users")])
def test_nova_rejects_writes_and_actions_before_transport(method, path):
    client = BackofficeClient("https://admin.test", "u", "p", transport=httpx.MockTransport(lambda _: pytest.fail("network called")))
    with pytest.raises(ValueError):
        asyncio.run(client._request(method, path))


def test_nova_csrf_419_starts_new_login_form():
    posts, gets = [], []
    def handle(request):
        if request.url.path == "/login":
            if request.method == "GET":
                gets.append(1)
                return httpx.Response(200, text='<input name="_token" value="new">')
            posts.append(1)
            return httpx.Response(419 if len(posts) == 1 else 302)
        return httpx.Response(200, json={"resource": nova_resource(is_hidden=True)})
    client = BackofficeClient("https://admin.test", "u", "p", transport=httpx.MockTransport(handle))
    assert asyncio.run(client.get_release(1))["id"] == 1
    assert len(gets) == len(posts) == 2


@pytest.mark.parametrize("payload", [{"resources": []}, {"resources": [], "next_page_url": "/nova-api/anime-releases?page=2"}, {"resources": [], "next_page_url": None, "total": 1}])
def test_nova_incomplete_pagination_is_an_error(payload):
    client = BackofficeClient("https://admin.test", "u", "p", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    client._authenticated = True
    with pytest.raises(BackofficeError):
        asyncio.run(client.list_resources("anime-releases"))


def test_nova_pages_all_resources_and_rejects_external_next_url():
    def handle(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"resources": [nova_resource(page)], "next_page_url": "/nova-api/anime-releases?page=2" if page == 1 else None, "total": 2})
    client = BackofficeClient("https://admin.test", "u", "p", transport=httpx.MockTransport(handle))
    client._authenticated = True
    assert len(asyncio.run(client.list_resources("anime-releases"))) == 2
    client._transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"resources": [nova_resource()], "next_page_url": "https://other.test/nova-api/anime-releases?page=2"}))
    with pytest.raises(BackofficeError):
        asyncio.run(client.list_resources("anime-releases"))


def test_nova_members_require_release_scope_and_reject_duplicate_uuid():
    from app.providers.anilibria.backoffice import MEMBERS_RESOURCE
    client = BackofficeClient("https://admin.test","u","p",transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")))
    with pytest.raises(ValueError, match="связь релиза"):
        asyncio.run(client.list_resources(MEMBERS_RESOURCE))
    client._authenticated = True
    client._transport = httpx.MockTransport(lambda _: httpx.Response(200,json={
        "resources":[nova_resource("00000000-0000-4000-8000-000000000001")],
        "next_page_url":"/nova-api/anime-releases-releasesMembers?page=2", "total":2}))
    with pytest.raises(BackofficeError,match="повтор id"):
        asyncio.run(client.list_resources(MEMBERS_RESOURCE,viaResource="anime-releases",viaResourceId=1,viaRelationship="members"))


def test_nova_computed_fields_are_disambiguated_and_sparse_meta_preserved():
    resource = nova_resource(hash=HASH, description="1-2")
    resource["fields"] += [
        {"attribute": "release", "component": "belongs-to-field", "belongsToId": 7},
        {"attribute": "ComputedField", "component": "text-field", "value": "2 GB"},
        {"attribute": "ComputedField", "component": "file-field", "label": "Correct name"},
        {"attribute": "ComputedField", "component": "url-field", "value": "magnet:?dn=Different"},
        {"attribute": "codec", "component": "select-field", "value": "x265/HEVC"},
    ]
    item = normalize_torrent(resource)
    assert item["label"] == "Correct name" and item["release_id"] == 7
    assert item["codec"]["value"] == "x265/HEVC"
    assert item["codec"]["label"] == "HEVC"
    release = normalize_release(nova_resource(name="Title", is_hidden=True))
    assert "members" not in release and "genres" not in release


@pytest.mark.parametrize("codec,label", [("x264/AVC", "AVC"), ("x265/HEVC", "HEVC"), ("AV1", "AV1")])
def test_nova_codec_labels_match_public_api(codec, label):
    from app.services.torrent_archive import TorrentArchiveService
    from app.services.torrent_qb_meta import build_qb_torrent_name_from_payloads
    from app.services.hevc_pairing import classify_archive_codec

    resource = nova_resource(hash=HASH, description="1-2")
    resource["fields"] += [
        {"attribute": "type", "component": "select-field", "value": "WEBRip"},
        {"attribute": "quality", "component": "select-field", "value": "1080p"},
        {"attribute": "codec", "component": "select-field", "value": codec},
    ]
    item = normalize_torrent(resource)
    assert item["codec"] == {"value": codec, "label": label, "description": label}
    torrent_type = TorrentArchiveService.extract_torrent_type(item)
    assert torrent_type == f"WEBRip 1080p {label}"
    assert classify_archive_codec(quality_json=item, torrent_type=torrent_type) == label
    assert build_qb_torrent_name_from_payloads(release_payload={"name": {"main": "Title"}}, torrent_payload=item).endswith(f"[WEBRip 1080p {label}]")


def source_client(public_payload, admin_payload=None, db=None):
    public = SimpleNamespace(get_release=AsyncMock(), get_torrents_for_release=AsyncMock(return_value=[{"id": 10, "hash": HASH}]),
        get_torrent=AsyncMock(return_value=None), download_torrent_file=AsyncMock(return_value=RAW), passkey="current-key", base_url="https://site.test")
    if isinstance(public_payload, Exception):
        public.get_release.side_effect = public_payload
    else:
        public.get_release.return_value = public_payload
    admin = SimpleNamespace(get_release=AsyncMock(return_value=admin_payload), get_torrents_for_release=AsyncMock(return_value=[{"id": 10, "hash": HASH, "release_id": 1}]),
        download_torrent_file=AsyncMock(return_value=RAW), get_torrent=AsyncMock(return_value={"id": 10, "hash": HASH, "release_id": 1}))
    return ReleaseSourceClient(public, admin, db)


@pytest.mark.parametrize("countries,expected", [(None, False), ([], False), (["RU"], True), (["RU", "BY"], True)])
@pytest.mark.parametrize("status", ["is_hidden", "is_blocked_by_copyrights"])
def test_nova_geo_multiselect_does_not_prevent_hidden_or_blocked_release(countries, expected, status):
    resource = nova_resource(**{status: True})
    resource["fields"].append({"attribute": "is_blocked_by_geo", "component": "multiselect-field", "value": countries})
    normalized = normalize_release(resource)
    assert normalized["is_blocked_by_geo"] is expected
    client = source_client(AniLibriaNotFoundError(), normalized)
    payload = asyncio.run(client.get_release(1))
    assert payload["_source"] == "backoffice"
    assert source_tags(payload["_source"], payload.get("is_hidden"), payload.get("is_blocked_by_copyrights")) == [
        "hidden" if status == "is_hidden" else "blocked"
    ]


@pytest.mark.parametrize("value", [{"RU": True}, "RU", [None], [""]])
def test_nova_invalid_geo_multiselect_is_not_silently_accepted(value):
    resource = nova_resource(is_hidden=True)
    resource["fields"].append({"attribute": "is_blocked_by_geo", "component": "multiselect-field", "value": value})
    with pytest.raises(BackofficeError, match="список стран"):
        normalize_release(resource)


@pytest.mark.parametrize("extra", [{}, {"is_blocked_by_geo": True}])
def test_site_has_priority_without_reading_admin(extra):
    client = source_client({"id": 1, **extra})
    assert asyncio.run(client.get_release(1))["_source"] == "site"
    assert len(asyncio.run(client.get_torrents_for_release(1))) == 1
    client.backoffice.get_release.assert_not_awaited()
    client.backoffice.get_torrents_for_release.assert_not_awaited()


@pytest.mark.parametrize("public", [AniLibriaNotFoundError(), {"id": 1, "is_hidden": True}, {"id": 1, "is_blocked_by_copyrights": True}])
def test_hidden_and_copyright_select_admin(public):
    client = source_client(public, {"id": 1, "_source": "backoffice", "is_hidden": True, "is_blocked_by_copyrights": True})
    payload = asyncio.run(client.get_release(1))
    assert source_tags(payload["_source"], payload["is_hidden"], payload["is_blocked_by_copyrights"]) == ["hidden", "blocked"]


def test_public_failure_is_not_absence_and_does_not_read_admin():
    client = source_client(TimeoutError(), {"id": 1, "is_hidden": True})
    with pytest.raises(TimeoutError):
        asyncio.run(client.get_release(1))
    client.backoffice.get_release.assert_not_awaited()


def source_client_with_transport(monkeypatch, handler, *, retries=1):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def build_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    public = AniLibriaClient(
        "https://primary.test/api/v1", "https://fallback.test/api/v1",
        request_retries=retries, retry_delay_ms=0,
    )
    admin = BackofficeClient("https://admin.test", "user", "password", retries=1, transport=transport)
    admin._authenticated = True
    return ReleaseSourceClient(public, admin)


@pytest.mark.parametrize("primary_statuses", [(404,), (503, 404)])
def test_primary_not_found_reaches_admin_without_querying_queue_fallback(monkeypatch, primary_statuses):
    calls = []
    statuses = iter(primary_statuses)

    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "primary.test":
            return httpx.Response(next(statuses), json={"message": "not found"})
        if request.url.host == "fallback.test":
            return httpx.Response(200, headers={"Content-Type": "application/octet-stream"},
                                  content=b"{ 'data': 'queue accepted, OK' }")
        assert request.url.host == "admin.test"
        assert request.url.path == "/nova-api/anime-releases/386"
        return httpx.Response(200, json={"resource": nova_resource(386, is_hidden=True)})

    client = source_client_with_transport(monkeypatch, handler, retries=len(primary_statuses))
    assert asyncio.run(client.get_release(386))["_source"] == "backoffice"
    assert calls == [("primary.test", "/api/v1/anime/releases/386")] * len(primary_statuses) + [
        ("admin.test", "/nova-api/anime-releases/386")
    ]


@pytest.mark.parametrize("primary_failure", ["http503", "timeout"])
def test_public_failure_then_fallback_not_found_does_not_select_admin(monkeypatch, primary_failure):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            if primary_failure == "timeout":
                raise httpx.ReadTimeout("mock timeout", request=request)
            return httpx.Response(503)
        assert request.url.host == "fallback.test"
        return httpx.Response(404, json={"message": "not found"})

    client = source_client_with_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client.get_release(386))
    assert type(exc.value) is RuntimeError
    assert calls == ["primary.test", "fallback.test"]


def test_valid_primary_release_does_not_query_fallback_or_admin(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        assert request.url.host == "primary.test"
        return httpx.Response(200, json={"id": 10364, "alias": "cyberpunk-2"})

    client = source_client_with_transport(monkeypatch, handler)
    assert asyncio.run(client.get_release(10364))["_source"] == "site"
    assert calls == ["primary.test"]


def test_geo_only_admin_is_not_eligible():
    client = source_client(AniLibriaNotFoundError(), {"id": 1, "is_blocked_by_geo": True})
    with pytest.raises(AniLibriaNotFoundError):
        asyncio.run(client.get_release(1))


def test_soft_deleted_detail_200_is_not_active():
    client = source_client(AniLibriaNotFoundError(), {"id": 1, "_source": "backoffice", "is_hidden": True})
    client.backoffice.get_torrents_for_release.return_value = []
    pipeline = SimpleNamespace(release_id=1, torrent_id=10, info_hash=HASH)
    assert not asyncio.run(client.pipeline_torrent_exists(pipeline))


def test_old_hash_same_id_is_not_valid_for_retry():
    client = source_client({"id": 1})
    assert not asyncio.run(client.pipeline_torrent_exists(SimpleNamespace(release_id=1, torrent_id=10, info_hash="f" * 40)))


def test_tracker_replacement_keeps_raw_info_and_replaces_both_tracker_fields():
    prepared = replace_trackers(RAW, "current-key")
    assert torrent_info_hash(prepared) == HASH
    assert raw_torrent_fields(prepared)[b"info"] == raw_torrent_fields(RAW)[b"info"]
    decoded, _ = _decode(prepared)
    urls = [tier[0].decode() for tier in decoded[b"announce-list"]]
    assert urls == tracker_urls("current-key")
    assert len(set(urls)) == len(BACKOFFICE_TRACKERS) == 15
    assert decoded[b"announce"] == urls[0].encode()
    assert b"http://old" not in prepared
    assert replace_trackers(prepared, "current-key") == prepared


@pytest.mark.parametrize("data", [b"", b"d", b"d4:info", b"d4:infode", RAW + b"junk"])
def test_bad_torrent_fails_without_hanging(data):
    with pytest.raises((ValueError, IndexError)):
        replace_trackers(data, "key")


def test_empty_passkey_is_an_error():
    with pytest.raises(ValueError):
        replace_trackers(RAW, "")


def test_download_hash_checked_and_backoffice_trackers_applied():
    client = source_client(AniLibriaNotFoundError(), {"id": 1, "_source": "backoffice", "is_hidden": True})
    async def run():
        await client.get_torrents_for_release(1)
        return await client.download_torrent_file(10)
    prepared = asyncio.run(run())
    assert torrent_info_hash(prepared) == HASH and b"pk=current-key" in prepared
    assert client._torrents[1][0]["size"] == 1
    client.backoffice.download_torrent_file.return_value = RAW.replace(b"test", b"else")
    with pytest.raises(RuntimeError, match="Hash"):
        asyncio.run(client.download_torrent_file(10))


class Qb:
    def __init__(self):
        self.tags = {"Комедия", "manual", "site"}
        self.trackers = ["http://old"]
    def torrents_info(self, **kwargs):
        return [SimpleNamespace(tags=", ".join(sorted(self.tags)))]
    def torrents_add_tags(self, *, tags, **kwargs):
        self.tags.update(tags)
    def torrents_remove_tags(self, *, tags, **kwargs):
        self.tags.difference_update(tags)
    def torrents_trackers(self, **kwargs):
        return [{"url": "** [DHT] **"}] + [{"url": url} for url in self.trackers]
    def torrents_add_trackers(self, *, urls, **kwargs):
        self.trackers.extend(urls.split("\n"))
    def torrents_remove_trackers(self, *, urls, **kwargs):
        self.trackers = [url for url in self.trackers if url not in urls.split("|")]


def test_qb_source_tags_preserve_genres_custom_tags_and_remove_obsolete_statuses():
    qb = Qb()
    assert reconcile_source_tags(qb, HASH, ["hidden", "blocked"])
    assert qb.tags == {"Комедия", "manual", "hidden", "blocked"}
    assert reconcile_source_tags(qb, HASH, ["site"])
    assert qb.tags == {"Комедия", "manual", "site"}
    assert not reconcile_source_tags(qb, HASH, ["site"])
    assert reconcile_trackers(qb, HASH, "key")
    assert qb.trackers == tracker_urls("key")
    assert not reconcile_trackers(qb, HASH, "key")


def add_archive(db, rid=1, **kwargs):
    row = TorrentArchive(release_id=rid, torrent_id=rid * 10, info_hash=HASH, file_path=f"{rid}.torrent", quality_json={}, **kwargs)
    db.add(row)
    db.commit()
    return row


def test_return_to_site_keeps_modified_trackers_and_manual_flag(db, tmp_path):
    path = tmp_path / "current.torrent"
    path.write_bytes(RAW)
    row = TorrentArchive(release_id=1, torrent_id=10, info_hash=HASH, file_path=str(path), quality_json={})
    db.add(row)
    db.add(Release(release_id=1, source="backoffice", is_hidden=True, is_abandoned=True))
    db.commit()
    qb = Qb()
    apply_release_policy(db, 1, [("master", qb)], "key")
    assert row.backoffice_trackers and "hidden" in qb.tags
    assert b"http://old" not in path.read_bytes()
    upsert_release_meta(db, 1, {"id": 1, "_source": "site", "is_hidden": False, "is_blocked_by_copyrights": False})
    apply_release_policy(db, 1, [("master", qb)], "key")
    assert row.backoffice_trackers and "site" in qb.tags and "hidden" not in qb.tags
    assert qb.trackers == tracker_urls("key")
    assert db.get(Release, 1).is_abandoned is True


@pytest.mark.parametrize("filters,expected", [({"source_filter":"backoffice"}, [2]), ({"source_filter":"site"}, [1]),
    ({"block_filter":"hidden"}, [2]), ({"block_filter":"copyright"}, [2]), ({"block_filter":"geo"}, [1]),
    ({"abandoned_filter":"yes"}, [2]), ({"abandoned_filter":"no"}, [1,3]), ({"source_filter":"unknown"}, [3]),
    ({"source_filter":"backoffice", "abandoned_filter":"no"}, [])])
def test_source_block_and_abandoned_filters(db, filters, expected):
    for rid in (1,2,3):
        add_archive(db, rid=rid)
    db.add_all([Release(release_id=1, source="site", is_blocked_by_geo=True),
                Release(release_id=2, source="backoffice", is_hidden=True, is_blocked_by_copyrights=True, is_abandoned=True)])
    db.commit()
    result = list_release_groups(db, **filters)
    assert sorted(g.release_id for g in result["groups"]) == expected
    assert result["total"] == len(expected)


def test_initial_sync_archives_hidden_release_and_retry_then_return_to_site(db, monkeypatch, tmp_path):
    from app.jobs import backoffice_sync as job
    from app.services.runtime_settings import get_setting_value
    from app.db.models import SeenTorrent, TorrentPipeline
    from app.services.hevc_notifications import _baseline_only
    from app.jobs.waiting_master_retry import torrent_still_in_api
    db.add(Setting(key="anilibria_passkey", value="current-key"))
    db.add(Setting(key="scrape_pause_sec", value="0"))
    db.commit()
    client = source_client(AniLibriaNotFoundError(), {"id": 1, "_source":"backoffice", "alias":"show", "name":{"main":"Title"}, "is_hidden":True})
    client.db = db
    client.public.catalog_releases = AsyncMock(return_value={"data": [], "meta": {"pagination": {"total": 0, "total_pages": 0}}})
    client.backoffice.list_resources = AsyncMock(return_value=[nova_resource()])
    client.backoffice.get_torrents_for_release.return_value = [{"id":10, "release_id":1, "hash":HASH, "codec":{"label":"AVC"}}]
    monkeypatch.setenv("TORRENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
    asyncio.run(job.run_backoffice_sync(db, 1, {}))
    client.public.catalog_releases.assert_awaited_once()
    archive = db.scalar(select(TorrentArchive))
    pipeline = db.scalar(select(TorrentPipeline))
    assert archive.backoffice_trackers and archive.notification_baseline
    assert archive.api_present and not archive.superseded
    assert pipeline.status == "waiting_master"
    assert db.get(SeenTorrent, 10).info_hash == HASH
    assert _baseline_only(db, 1)
    assert get_setting_value(db, "backoffice_baseline_completed_at")
    assert asyncio.run(torrent_still_in_api(client, pipeline))
    client.backoffice.get_release.reset_mock()
    # Следующий обход: тот же ID/hash снова на сайте, не новая версия и не новый SLA.
    before_created = archive.created_at
    client._releases.clear()
    client._torrents.clear()
    client.public.get_release.side_effect = None
    client.public.get_release.return_value = {"id":1, "alias":"show", "name":{"main":"Title"}}
    client.public.catalog_releases.return_value = {"data": [{"id":1}], "meta": {"pagination": {"total": 1, "total_pages": 1}}}
    asyncio.run(job.run_backoffice_sync(db, 2, {}))
    db.refresh(archive)
    assert db.get(Release, 1).source == "site"
    assert archive.backoffice_trackers and archive.notification_baseline
    assert archive.created_at == before_created
    assert db.scalar(select(TorrentArchive.id)) == archive.id
    assert b"pk=current-key" in (tmp_path / (HASH + ".torrent")).read_bytes()
    # Ещё один обход после возврата: политика должна повторяться и при source=site.
    client._releases.clear()
    client._torrents.clear()
    client.public.get_release.reset_mock()
    asyncio.run(job.run_backoffice_sync(db, 3, {}))
    client.public.get_release.assert_awaited_once_with(1)
    assert client.backoffice.get_release.await_count == 2


def test_real_processor_archives_known_backoffice_during_public_outage(db, monkeypatch, tmp_path):
    from app.jobs import backoffice_sync as job
    from app.services.runtime_settings import get_setting_value
    confirmed = upsert_release_meta(db, 1, {"id": 1, "_source": "backoffice",
        "is_hidden": True, "is_blocked_by_copyrights": False})
    checked_at = confirmed.source_checked_at
    db.add(Setting(key="anilibria_passkey", value="current-key"))
    db.add(Setting(key="scrape_pause_sec", value="0"))
    db.commit()
    client = source_client(RuntimeError("API unavailable"), {"id": 1, "_source": "backoffice",
        "alias": "show", "name": {"main": "Fresh Nova title"},
        "is_hidden": False, "is_blocked_by_copyrights": True}, db)
    client.public.catalog_releases = AsyncMock(side_effect=RuntimeError("public catalog unavailable"))
    client.backoffice.list_resources = AsyncMock(return_value=[nova_resource()])
    monkeypatch.setenv("TORRENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)

    asyncio.run(job.run_backoffice_sync(db, 1, {}))

    db.refresh(confirmed)
    assert (confirmed.source, confirmed.source_checked_at) == ("backoffice", checked_at)
    assert confirmed.is_hidden is True and confirmed.is_blocked_by_copyrights is False
    assert confirmed.title == "Fresh Nova title"
    archive = db.scalar(select(TorrentArchive))
    assert archive.info_hash == HASH and archive.api_present
    assert archive.backoffice_trackers and archive.notification_baseline
    assert archive.quality_json["_source"] == "backoffice"
    assert archive.quality_json["is_hidden"] is True
    assert archive.quality_json["is_blocked_by_copyrights"] is False
    client.public.catalog_releases.assert_not_awaited()
    client.public.get_torrents_for_release.assert_not_awaited()
    client.backoffice.download_torrent_file.assert_awaited_once_with(10)
    assert get_setting_value(db, "backoffice_baseline_completed_at")


def test_partial_import_does_not_complete_baseline(db, monkeypatch):
    from app.jobs import backoffice_sync as job
    from app.services.runtime_settings import get_setting_value
    client = source_client(AniLibriaNotFoundError(), {"id":1, "_source":"backoffice", "is_hidden":True}, db)
    client.public.catalog_releases = AsyncMock(return_value={"data": [], "meta": {"pagination": {"total": 0, "total_pages": 0}}})
    client.backoffice.list_resources = AsyncMock(return_value=[nova_resource()])
    db.add(Setting(key="scrape_pause_sec", value="0"))
    db.commit()
    monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
    processor = SimpleNamespace(process_release=AsyncMock(return_value={"errors":1}))
    monkeypatch.setattr(job, "TorrentProcessor", lambda **kwargs: processor)
    with pytest.raises(RuntimeError, match="Завершено с ошибками"):
        asyncio.run(job.run_backoffice_sync(db, 1, {}))
    assert get_setting_value(db, "backoffice_baseline_started_at")
    assert not get_setting_value(db, "backoffice_baseline_completed_at")


def test_broken_public_catalog_does_not_block_empty_nova_scan(db, monkeypatch):
    from app.jobs import backoffice_sync as job
    from app.services.runtime_settings import get_setting_value
    client = source_client({}, db=db)
    client.public.catalog_releases = AsyncMock(return_value={"error":"broken"})
    client.backoffice.list_resources = AsyncMock(return_value=[])
    monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
    asyncio.run(job.run_backoffice_sync(db, 1, {}))
    client.public.catalog_releases.assert_not_awaited()
    client.backoffice.list_resources.assert_awaited_once()
    assert get_setting_value(db, "backoffice_baseline_completed_at")


def test_sync_logs_backoffice_error_details_and_redacts_secrets(db, monkeypatch):
    from app.db.models import JobLog
    from app.jobs import backoffice_sync as job
    client = source_client(AniLibriaNotFoundError(), db=db)
    client.public.catalog_releases = AsyncMock(return_value={"data": [], "meta": {"pagination": {"total": 0, "total_pages": 0}}})
    client.backoffice.list_resources = AsyncMock(return_value=[nova_resource()])
    client.backoffice.get_release.side_effect = BackofficeError("Backoffice: некорректный is_hidden; ?pk=private-key")
    monkeypatch.setattr(job, "build_anilibria_client", lambda _: client)
    monkeypatch.setattr(job, "is_stop_requested", lambda *args: False)
    with pytest.raises(RuntimeError, match="Завершено с ошибками"):
        asyncio.run(job.run_backoffice_sync(db, 1, {}))
    warning = db.scalar(select(JobLog.message).where(JobLog.level == "warning"))
    assert "BackofficeError: Backoffice: некорректный is_hidden" in warning
    assert "private-key" not in warning


def test_manual_abandoned_toggle_is_local_and_survives_sync(db, monkeypatch):
    from app.main import templates, toggle_release_abandoned
    monkeypatch.setattr(templates, "context_processors", [])
    add_archive(db)
    response = toggle_release_abandoned(MagicMock(), 1, "on", db)
    assert "checked" in response.body.decode()
    assert db.get(Release, 1).is_abandoned
    upsert_release_meta(db, 1, {"id":1, "_source":"backoffice", "is_hidden":True})
    assert db.get(Release, 1).is_abandoned
    toggle_release_abandoned(MagicMock(), 1, None, db)
    assert not db.get(Release, 1).is_abandoned


def test_settings_save_and_display_independent_admin_api_url(db, monkeypatch):
    import inspect
    from starlette.requests import Request
    from app.core.config import settings
    from app.main import settings_page, templates, update_settings
    from app.services.runtime_settings import build_anilibria_client

    monkeypatch.setattr(templates, "context_processors", [])
    monkeypatch.setattr(settings, "anilibria_admin_base_url", "https://env-admin.test")
    request = Request({"type": "http", "method": "POST", "path": "/settings", "headers": []})
    fields = {name: param.default.default for name, param in inspect.signature(update_settings).parameters.items()
              if name not in {"request", "db"}}
    fields.update(anilibria_admin_base_url=" https://admin.test ", anilibria_admin_url_template="")
    response = update_settings(request, db=db, **fields)
    assert response.status_code == 200
    assert db.get(Setting, "anilibria_admin_base_url").value == "https://admin.test"
    assert build_anilibria_client(db).backoffice.base_url == "https://admin.test"
    assert 'name="anilibria_admin_base_url" value="https://admin.test"' in settings_page(request, db).body.decode()
    # Явное пустое значение отключает источник даже при заданном env.
    fields["anilibria_admin_base_url"] = ""
    update_settings(request, db=db, **fields)
    assert not isinstance(build_anilibria_client(db), ReleaseSourceClient)


def test_scoped_genre_sync_keeps_manual_and_source_tags_but_removes_old_genre():
    from app.services.qbittorrent import _ensure_torrent_tags
    qb = Qb()
    assert _ensure_torrent_tags(qb, HASH, ["Драма"], managed_tags=["Комедия", "Драма"], delay_sec=0)
    assert qb.tags == {"manual", "site", "Драма"}


def test_partial_initial_import_keeps_baseline_for_remaining_torrents(db):
    archive = add_archive(db, notification_baseline=True)
    client = source_client(AniLibriaNotFoundError(), {"id":1, "_source":"backoffice", "is_hidden":True}, db)
    assert asyncio.run(client.get_release(1))["_notification_baseline"]
    # Старый обычный релиз до первого обхода не становится baseline.
    archive.notification_baseline = False
    db.commit()
    client._releases.clear()
    assert "_notification_baseline" not in asyncio.run(client.get_release(1))


def test_baseline_suppresses_deferred_hevc_alerts_until_new_avc(db, monkeypatch):
    from app.services import hevc_notifications as notifications
    archive = add_archive(db, notification_baseline=True, torrent_type="AVC")
    monkeypatch.setattr(notifications, "list_approved_group_ids", lambda *args, **kwargs: pytest.fail("baseline alert"))
    assert notifications.enqueue_new_release_notification(db, release_id=1, info_hash=HASH, hashed_files=1) == 0
    assert notifications._enqueue_release_notifications(db, release_id=1, kind="hevc_missing_sla", heading="") == 0
    assert notifications._baseline_only(db, 1)
    archive.notification_baseline = False
    db.commit()
    assert not notifications._baseline_only(db, 1)


def test_both_telegram_profiles_show_offsite_title_without_site_link():
    from app.services.hevc_bot import HevcReleaseStatus, _release_link
    from app.services.telegram_notify import build_torrent_notification_text, build_file_changes_notification_text
    row = HevcReleaseStatus(1, "alias", "Title <test>", None, [], offsite=True)
    text = _release_link(row)
    assert "Нет на сайте" in text and "<a " not in text and "&lt;test&gt;" in text
    for text in (
        build_torrent_notification_text(title="Title", alias="alias", torrents=[], offsite=True),
        build_file_changes_notification_text(title="Title", alias="alias", torrent_label="AVC", changes=[], offsite=True),
    ):
        assert "Нет на сайте" in text and "](" not in text


def test_retry_loads_exact_archived_hash_and_applies_new_backoffice_source(db, tmp_path):
    from app.services.pipeline import TorrentPipelineService
    path = tmp_path / "old.torrent"
    path.write_bytes(RAW)
    archive = add_archive(db, backoffice_trackers=False)
    archive.file_path = str(path)
    newer = TorrentArchive(release_id=1, torrent_id=10, info_hash="f" * 40,
                           file_path=str(tmp_path / "missing.torrent"), quality_json={})
    db.add_all([newer, Release(release_id=1, source="backoffice", is_hidden=True),
                Setting(key="anilibria_passkey", value="key")])
    db.commit()
    pipeline = SimpleNamespace(id=1, info_hash=HASH, release_id=1, torrent_id=10)
    service = TorrentPipelineService(db)
    prepared = service.load_torrent_bytes_from_archive(pipeline)
    assert prepared and torrent_info_hash(prepared) == HASH and b"pk=key" in prepared
    assert archive.backoffice_trackers and not newer.backoffice_trackers
    assert service._resolve_qb_meta(pipeline)[3] == ["hidden"]


def test_source_migration_preserves_old_rows_and_can_downgrade(monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    spec = importlib.util.spec_from_file_location("backoffice_migration", Path(__file__).parents[1] / "alembic/versions/0024_backoffice_source.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE releases (release_id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE torrent_archive (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO releases VALUES (1)"))
        connection.execute(text("INSERT INTO torrent_archive VALUES (1)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.execute(text("SELECT source, is_abandoned FROM releases")).one() == (None, 0)
        assert connection.execute(text("SELECT backoffice_trackers, notification_baseline FROM torrent_archive")).one() == (0, 0)
        migration.downgrade()
        assert [column["name"] for column in inspect(connection).get_columns("releases")] == ["release_id"]
        assert connection.execute(text("SELECT id FROM torrent_archive")).scalar_one() == 1
    engine.dispose()


@pytest.mark.parametrize("html", [False, True])
def test_queued_telegram_links_follow_current_source_in_both_directions(db, html):
    from app.services.telegram_outbox import refresh_source_link
    from app.services.telegram_notify import build_torrent_notification_text
    from app.services.hevc_bot import _release_link
    if html:
        link = _release_link(SimpleNamespace(title="Title", alias="alias", offsite=False))
        payload = {"release_id":1, "title":"Title", "alias":"alias", "release_link":link,
                   "text":"Событие\n" + link + "\nСнимок деталей"}
    else:
        payload = {"release_id":1, "title":"Title", "alias":"alias", "torrents":[],
                   "text":build_torrent_notification_text(title="Title", alias="alias", torrents=[])}
    release = Release(release_id=1, source="backoffice", is_hidden=True)
    db.add(release)
    db.commit()
    hidden_text = refresh_source_link(db, payload)["text"]
    assert "Нет на сайте" in hidden_text and "aniliberty" not in hidden_text
    if html:
        assert "Снимок деталей" in hidden_text
    release.source = "site"
    db.commit()
    assert refresh_source_link(db, payload)["text"] == payload["text"]


@pytest.mark.parametrize("ref", ["1", "show", "https://admin.test/nova/resources/anime-releases/1",
                                  "https://site.test/anime/releases/release/1"])
def test_telegram_add_duplicate_and_del_use_same_release_reference(db, monkeypatch, ref):
    from contextlib import nullcontext
    from app.telegram_bot import handlers
    from app.db.models import TrackedRelease
    client = source_client({"id":1, "alias":"show", "name":{"main":"Title"}}, db=db)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(handlers, "check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "build_anilibria_client", lambda _: client)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(args=[ref])
    asyncio.run(handlers.add_alias(update, context))
    assert db.get(TrackedRelease, 1).release_alias == "show"
    assert db.get(TrackedRelease, 1).enabled
    client.public.get_release.reset_mock()
    asyncio.run(handlers.add_alias(update, context))
    assert "уже отслеживается" in update.message.reply_text.call_args.args[0]
    client.public.get_release.assert_not_awaited()
    asyncio.run(handlers.del_alias(update, context))
    assert not db.get(TrackedRelease, 1).enabled
    assert "Отключен" in update.message.reply_text.call_args.args[0]


def test_scoped_meta_sync_applies_source_policy_under_lock_to_both_qb_clients(db, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from app.db.models import SeenTorrent
    from app.jobs.meta_sync import _process_release_or_meta
    from app.services.torrent_processor import TorrentProcessor
    from app.services import release_source
    path = tmp_path / "current.torrent"
    path.write_bytes(RAW)
    archive = add_archive(db)
    archive.file_path = str(path)
    archive.quality_json = {"_source":"site"}
    db.add_all([Release(release_id=1, source="site"), SeenTorrent(torrent_id=10, info_hash=HASH),
                Setting(key="anilibria_passkey", value="current-key")])
    db.commit()
    client = source_client(AniLibriaNotFoundError(), {"id":1, "alias":"show", "_source":"backoffice",
                           "is_hidden":True, "is_blocked_by_copyrights":False}, db)
    processor = TorrentProcessor(db=db, job_id=1, client=client)
    master, slave = Qb(), Qb()
    monkeypatch.setattr(processor, "_qb_clients_for_meta", lambda: [("master", master), ("slave", slave)])
    for method in ("_refresh_qb_comments", "_refresh_qb_tags", "_refresh_qb_renames"):
        monkeypatch.setattr(processor, method, lambda **kwargs: 0)
    held, locks, policies = [], [], []
    @contextmanager
    def lock(session, rid):
        assert not held
        held.append(rid)
        locks.append(rid)
        try:
            yield
        finally:
            held.pop()
    original = release_source.apply_release_policy
    def policy(session, rid, *args, **kwargs):
        assert held == [rid]
        policies.append(rid)
        return original(session, rid, *args, **kwargs)
    monkeypatch.setattr("app.services.release_lock.release_lock", lock)
    monkeypatch.setattr(release_source, "apply_release_policy", policy)
    for desired in ("hidden", "blocked", "site"):
        client._releases.clear()
        client._torrents.clear()
        if desired == "blocked":
            client.backoffice.get_release.return_value = {"id":1, "alias":"show", "_source":"backoffice",
                "is_hidden":False, "is_blocked_by_copyrights":True}
        if desired == "site":
            client.public.get_release.side_effect = None
            client.public.get_release.return_value = {"id":1, "alias":"show"}
        asyncio.run(_process_release_or_meta(processor, release_id=1, release_alias="show", torrent_id_filter=10))
        for qb in (master, slave):
            assert qb.tags & {"site", "hidden", "blocked"} == {desired}
            assert "manual" in qb.tags and "Комедия" in qb.tags
            assert qb.trackers == tracker_urls("current-key")
        assert archive.backoffice_trackers and torrent_info_hash(path.read_bytes()) == HASH
    assert locks == policies == [1, 1, 1]
    assert len(db.scalars(select(TorrentArchive)).all()) == 1
