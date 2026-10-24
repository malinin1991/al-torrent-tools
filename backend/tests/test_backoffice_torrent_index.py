"""Sanitized real Nova index shape; absent hashes require periodic detail rechecks."""

import asyncio
from copy import deepcopy

import httpx
import pytest

from app.providers.anilibria.backoffice import (
    BackofficeClient, BackofficeError, resource_id, torrent_index_fingerprint,
    torrent_index_peer_counts, torrent_index_snapshot,
)


RELEASE_ID = 10317
TORRENT_ID = 40514
HASH_A = "a" * 40
HASH_B = "b" * 40


def index_resource(torrent_id=TORRENT_ID):
    """Relevant field layout from Nova's relationship index; no URLs or auth."""
    return {
        "id": {"attribute": "id", "component": "id-field", "value": torrent_id},
        "title": "WEB-DL • 1080p AVC • 1-3",
        "fields": [
            {"attribute": "", "component": "action-button", "action": {
                "name": "Редактировать", "fields": [
                    {"attribute": "description", "component": "text-field", "value": "1-3"},
                    {"attribute": "file", "component": "file-field", "value": None},
                ],
            }},
            {"attribute": "название", "component": "stack-field", "lines": [
                {"attribute": "ComputedField", "component": "text-field", "name": "Название",
                 "value": "WEB-DL • 1080p AVC • 1-3"},
                {"attribute": "release", "component": "belongs-to-field", "belongsToId": RELEASE_ID,
                 "value": "Example release"},
            ]},
            {"attribute": "seeders", "component": "text-field", "value": 18},
            {"attribute": "leechers", "component": "text-field", "value": 11},
            {"attribute": "completed_times", "component": "text-field", "value": 443},
            {"attribute": "ComputedField", "component": "text-field", "name": "Размер торрента",
             "value": "4.116 GB"},
            {"attribute": "updated_at", "component": "date-time-field", "value": "2026-10-24T12:22:13+00:00"},
        ],
    }


def detail_resource(torrent_id=TORRENT_ID, *, info_hash=HASH_A, release_id=RELEASE_ID):
    fields = [
        {"attribute": key, "component": "text-field", "value": value}
        for key, value in {
            "hash": info_hash, "description": "1-3", "seeders": 18, "leechers": 11,
            "created_at": "2026-10-24T12:22:13+00:00", "updated_at": "2026-10-24T12:22:13+00:00",
        }.items()
    ]
    fields.extend([
        {"attribute": "release", "component": "belongs-to-field", "belongsToId": release_id},
        {"attribute": "type", "component": "select-field", "value": "WEB-DL"},
        {"attribute": "quality", "component": "select-field", "value": "1080p"},
        {"attribute": "codec", "component": "select-field", "value": "x264/AVC"},
        {"attribute": "ComputedField", "component": "file-field", "label": "Example.torrent"},
    ])
    return {"id": {"value": torrent_id}, "fields": fields}


class MemoryCache:
    """The provider contract only; durable storage and TTL belong to its caller."""

    def __init__(self):
        self.entries = {}
        self.lookups = []
        self.remembered = []

    def lookup(self, resource, release_id):
        key = (resource_id(resource), release_id)
        self.lookups.append(key)
        fingerprint = torrent_index_fingerprint(resource, release_id)
        entry = self.entries.get(key)
        if fingerprint is not None and entry is not None and entry[0] == fingerprint:
            return entry[1]
        return None

    def remember(self, resource, release_id, payload):
        key = (resource_id(resource), release_id)
        fingerprint = torrent_index_fingerprint(resource, release_id)
        if fingerprint is not None:
            self.entries[key] = (fingerprint, deepcopy(payload))
            self.remembered.append(key)


def mock_client(resources, *, details=None, pages=None):
    calls = []
    details = details or {resource_id(item): detail_resource(resource_id(item)) for item in resources}

    def handle(request):
        assert request.method == "GET"
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/nova-api/anime-torrents":
            assert request.url.params["viaResourceId"] == str(RELEASE_ID)
            assert request.url.params["viaRelationship"] == "torrents"
            if pages is not None:
                return httpx.Response(200, json=pages[int(request.url.params["page"]) - 1])
            return httpx.Response(200, json={"resources": resources, "next_page_url": None, "total": len(resources)})
        prefix = "/nova-api/anime-torrents/"
        assert request.url.path.startswith(prefix)
        return httpx.Response(200, json={"resource": details[int(request.url.path[len(prefix):])]})

    client = BackofficeClient("https://admin.test", "", "", retries=1, transport=httpx.MockTransport(handle))
    client._authenticated = True
    return client, calls


def detail_calls(calls):
    return [path for path, _ in calls if path.startswith("/nova-api/anime-torrents/")]


def test_real_index_snapshot_reads_nested_fields_and_does_not_invent_hash():
    resource = index_resource()
    assert torrent_index_snapshot(resource, RELEASE_ID) == {
        "torrent_id": TORRENT_ID, "release_id": RELEASE_ID,
        "title": "WEB-DL • 1080p AVC • 1-3", "updated_at": "2026-10-24T12:22:13+00:00",
        "size_label": "4.116 GB", "description": "1-3",
    }
    assert torrent_index_peer_counts(resource) == {"seeders": 18, "leechers": 11}


def test_fingerprint_ignores_peers_completed_counts_and_action_display_metadata():
    resource = index_resource()
    before = torrent_index_fingerprint(resource, RELEASE_ID)
    changed = deepcopy(resource)
    for index in (2, 3, 4):
        changed["fields"][index]["value"] += 100
    changed["fields"][0]["action"].update(name="New action label", uriKey="new-action-ui-key")
    changed["fields"][1]["lines"][0]["value"] = "Different display HTML"
    assert torrent_index_fingerprint(changed, RELEASE_ID) == before


@pytest.mark.parametrize("marker", ["id", "title", "upload", "size", "description", "hash"])
def test_observable_version_markers_change_fingerprint(marker):
    resource = index_resource()
    before = torrent_index_fingerprint(resource, RELEASE_ID)
    if marker == "id":
        resource["id"]["value"] += 1
    elif marker == "title":
        resource["title"] = "WEB-DL • 1080p AVC • 1-4"
    elif marker == "upload":
        resource["fields"][6]["value"] = "2026-10-25T12:22:13+00:00"
    elif marker == "size":
        resource["fields"][5]["value"] = "4.117 GB"
    elif marker == "description":
        resource["fields"][0]["action"]["fields"][0]["value"] = "1-4"
    else:
        resource["fields"].append({"attribute": "hash", "component": "text-field", "value": HASH_B})
    assert torrent_index_fingerprint(resource, RELEASE_ID) not in (None, before)


@pytest.mark.parametrize("marker", ["title", "upload", "description", "size", "release", "hash"])
def test_missing_or_invalid_required_markers_do_not_authorize_cache(marker):
    resource = index_resource()
    if marker == "title":
        resource.pop("title")
    elif marker == "upload":
        resource["fields"][6]["value"] = "not-a-date"
    elif marker == "description":
        resource["fields"][0]["action"]["fields"][0]["value"] = None
    elif marker == "size":
        resource["fields"][5]["value"] = "unknown"
    elif marker == "release":
        resource["fields"][1]["lines"][1]["belongsToId"] += 1
    else:
        resource["fields"].append({"attribute": "hash", "component": "text-field", "value": "invalid"})
    assert torrent_index_snapshot(resource, RELEASE_ID) is None
    assert torrent_index_fingerprint(resource, RELEASE_ID) is None


def test_cache_hit_skips_detail_and_overlays_fresh_peer_counts_without_mutation():
    resource = index_resource()
    cache = MemoryCache()
    first, first_calls = mock_client([resource])
    original = asyncio.run(first.get_torrents_for_release(RELEASE_ID, cache=cache))[0]
    resource["fields"][2]["value"] = 22
    resource["fields"][3]["value"] = "7"
    second, second_calls = mock_client([resource])

    result = asyncio.run(second.get_torrents_for_release(RELEASE_ID, cache=cache))[0]

    assert len(detail_calls(first_calls)) == 1 and not detail_calls(second_calls)
    assert first.torrent_detail_reads == 1 and first.torrent_cache_hits == 0
    assert second.torrent_detail_reads == 0 and second.torrent_cache_hits == 1
    assert result["seeders"] == 22 and result["leechers"] == 7
    result["codec"]["label"] = "mutated"
    assert original["codec"]["label"] == "AVC"
    cached = cache.entries[(TORRENT_ID, RELEASE_ID)][1]
    assert cached["codec"]["label"] == "AVC" and cached["seeders"] == 18


def test_new_torrent_id_fetches_only_new_detail():
    cache = MemoryCache()
    first, _ = mock_client([index_resource()])
    asyncio.run(first.get_torrents_for_release(RELEASE_ID, cache=cache))
    resources = [index_resource(), index_resource(TORRENT_ID + 1)]
    second, calls = mock_client(resources)

    result = asyncio.run(second.get_torrents_for_release(RELEASE_ID, cache=cache))

    assert [item["id"] for item in result] == [TORRENT_ID, TORRENT_ID + 1]
    assert detail_calls(calls) == [f"/nova-api/anime-torrents/{TORRENT_ID + 1}"]


def test_upload_stamp_change_fetches_new_hash_for_existing_id():
    resource = index_resource()
    cache = MemoryCache()
    first, _ = mock_client([resource])
    asyncio.run(first.get_torrents_for_release(RELEASE_ID, cache=cache))
    resource["fields"][6]["value"] = "2026-10-25T12:22:13+00:00"
    second, calls = mock_client([resource], details={TORRENT_ID: detail_resource(info_hash=HASH_B)})

    result = asyncio.run(second.get_torrents_for_release(RELEASE_ID, cache=cache))

    assert len(detail_calls(calls)) == 1 and result[0]["hash"] == HASH_B


def test_missing_index_stamp_falls_back_to_detail_even_with_warm_cache():
    resource = index_resource()
    cache = MemoryCache()
    first, _ = mock_client([resource])
    asyncio.run(first.get_torrents_for_release(RELEASE_ID, cache=cache))
    resource["fields"].pop()
    second, calls = mock_client([resource])

    asyncio.run(second.get_torrents_for_release(RELEASE_ID, cache=cache))

    assert len(detail_calls(calls)) == 1
    assert len(cache.remembered) == 1


def test_failed_pagination_does_not_lookup_or_remember_partial_index():
    resource = index_resource()
    cache = MemoryCache()
    pages = [
        {"resources": [resource], "next_page_url": "/nova-api/anime-torrents?page=2", "total": 2},
        {"resources": [], "next_page_url": None, "total": 2},
    ]
    client, calls = mock_client([resource], pages=pages)

    with pytest.raises(BackofficeError, match="неполный список"):
        asyncio.run(client.get_torrents_for_release(RELEASE_ID, cache=cache))

    assert not cache.lookups and not cache.remembered and not detail_calls(calls)


@pytest.mark.parametrize("bad_detail", ["ownership", "hash", "id"])
def test_late_invalid_detail_does_not_remember_any_new_payload(bad_detail):
    resources = [index_resource(), index_resource(TORRENT_ID + 1)]
    bad = detail_resource(TORRENT_ID + 1)
    if bad_detail == "ownership":
        bad["fields"][6]["belongsToId"] = RELEASE_ID + 1
    elif bad_detail == "hash":
        bad["fields"][0]["value"] = "invalid"
    else:
        bad["id"]["value"] += 1
    cache = MemoryCache()
    client, _ = mock_client(resources, details={TORRENT_ID: detail_resource(), TORRENT_ID + 1: bad})

    with pytest.raises(BackofficeError):
        asyncio.run(client.get_torrents_for_release(RELEASE_ID, cache=cache))

    assert not cache.entries and not cache.remembered


def test_calls_without_cache_keep_fetching_details_from_sparse_index():
    sparse = {"id": {"value": TORRENT_ID}, "fields": []}
    client, calls = mock_client([sparse])

    asyncio.run(client.get_torrents_for_release(RELEASE_ID))
    asyncio.run(client.get_torrents_for_release(RELEASE_ID))

    assert len(detail_calls(calls)) == 2
