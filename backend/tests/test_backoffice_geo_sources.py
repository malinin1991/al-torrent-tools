"""Geo-only Nova fallback preserves public priority and its own import baseline."""

import asyncio
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Release, Setting, TorrentArchive
from app.providers.anilibria.backoffice import normalize_release
from app.providers.anilibria.client import AniLibriaNotFoundError
from app.providers.anilibria.sources import (
    BackofficeReleaseSkipped, ReleaseSourceClient, SourceCheckDeferred,
    backoffice_eligible, eligible,
)
from app.services.runtime_settings import get_setting_value


HASH_A = "a" * 40
HASH_B = "b" * 40
BASELINE_AT = "2026-10-20T08:30:00"
CHECKED_AT = datetime(2026, 10, 20, 8, 30)


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(*args, **kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Setting(key="backoffice_baseline_completed_at", value=BASELINE_AT))
        session.commit()
        yield session
    engine.dispose()


def geo_payload(release_id=1):
    return {"id": release_id, "alias": "geo-release", "name": {"main": "Geo release"},
            "_source": "backoffice", "is_hidden": False, "is_blocked_by_copyrights": False,
            "is_blocked_by_geo": True}


def make_client(db, *, release_id=1, public_result=None, admin_payload=None):
    public = SimpleNamespace(
        get_release=AsyncMock(), get_torrent=AsyncMock(return_value=None),
        get_torrents_for_release=AsyncMock(return_value=[{"id": 10, "hash": HASH_A}]),
    )
    if public_result is None:
        public_result = AniLibriaNotFoundError()
    if isinstance(public_result, Exception):
        public.get_release.side_effect = public_result
    else:
        public.get_release.return_value = public_result
    admin = SimpleNamespace(
        get_release=AsyncMock(return_value=admin_payload if admin_payload is not None else geo_payload(release_id)),
        get_torrents_for_release=AsyncMock(return_value=[{
            "id": 10, "release_id": release_id, "hash": HASH_A, "codec": {"label": "AVC"},
        }]),
        get_torrent=AsyncMock(return_value={"id": 10, "release_id": release_id, "hash": HASH_A}),
        list_resources=AsyncMock(return_value=[{"id": {"value": release_id}, "fields": []}]),
    )
    return ReleaseSourceClient(public, admin, db)


def mode(client, scan, *, watched=()):
    return client.backoffice_scan(watched) if scan else nullcontext()


def add_archive(db, *, baseline=False):
    archive = TorrentArchive(release_id=1, torrent_id=10, info_hash=HASH_A,
                             file_path="unused.torrent", quality_json={}, notification_baseline=baseline)
    db.add(archive)
    db.commit()
    return archive


@pytest.mark.parametrize("countries,expected", [(None, False), ([], False), (["RU"], True)])
def test_geo_eligibility_uses_normalized_nova_countries(countries, expected):
    resource = {"id": {"value": 1}, "fields": [
        {"attribute": "is_hidden", "component": "boolean-field", "value": False},
        {"attribute": "is_blocked_by_copyrights", "component": "boolean-field", "value": False},
        {"attribute": "is_blocked_by_geo", "component": "multiselect-field", "value": countries},
    ]}
    payload = normalize_release(resource)
    assert backoffice_eligible(payload) is expected
    assert not eligible(payload)


@pytest.mark.parametrize("scan", [False, True])
def test_public_geo_only_card_stays_site(db, scan):
    public = {**geo_payload(), "_source": "site"}
    client = make_client(db, public_result=public)

    with mode(client, scan, watched={1}):
        result = asyncio.run(client.get_release(1))

    assert result["_source"] == "site" and result["is_blocked_by_geo"] is True
    assert "_notification_baseline" not in result
    assert db.get(Release, 1).source == "site"
    if not scan:
        client.backoffice.get_release.assert_not_awaited()


@pytest.mark.parametrize("scan", [False, True])
def test_public_404_selects_geo_only_nova_and_reads_its_torrents(db, scan):
    client = make_client(db)

    with mode(client, scan):
        result = asyncio.run(client.get_release(1))
        torrents = asyncio.run(client.get_torrents_for_release(1))

    assert result["_source"] == "backoffice"
    assert db.get(Release, 1).is_blocked_by_geo is True
    assert torrents[0]["hash"] == HASH_A
    client.backoffice.get_torrents_for_release.assert_awaited_once_with(1)
    client.public.get_torrents_for_release.assert_not_awaited()


@pytest.mark.parametrize("scan", [False, True])
@pytest.mark.parametrize("geo", ["missing", None, False])
def test_missing_null_or_false_geo_does_not_make_ordinary_nova_eligible(db, scan, geo):
    payload = geo_payload()
    if geo == "missing":
        payload.pop("is_blocked_by_geo")
    else:
        payload["is_blocked_by_geo"] = geo
    client = make_client(db, admin_payload=payload)

    with mode(client, scan):
        with pytest.raises(BackofficeReleaseSkipped if scan else AniLibriaNotFoundError):
            asyncio.run(client.get_release(1))

    assert db.get(Release, 1) is None


def test_geo_only_alias_resolution_can_select_nova(db):
    client = make_client(db)
    result = asyncio.run(client.get_release("geo-release"))
    assert result["_source"] == "backoffice" and result["id"] == 1
    client.backoffice.list_resources.assert_awaited_once_with("anime-releases", search="geo-release")


def test_public_error_outside_scan_does_not_turn_geo_into_nova_fallback(db):
    error = RuntimeError("Public API unavailable")
    client = make_client(db, public_result=error)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client.get_release(1))

    assert exc.value is error
    client.backoffice.get_release.assert_not_awaited()
    assert db.get(Release, 1) is None


@pytest.mark.parametrize("source", [None, "site", "backoffice"])
def test_scan_public_error_retains_only_confirmed_backoffice(db, source):
    row = None
    if source is not None:
        row = Release(release_id=1, source=source, source_checked_at=CHECKED_AT,
                      is_hidden=False, is_blocked_by_copyrights=False, is_blocked_by_geo=True)
        db.add(row)
        db.commit()
    client = make_client(db, public_result=RuntimeError("Public API unavailable"))

    with client.backoffice_scan({1}):
        if source == "backoffice":
            result = asyncio.run(client.get_release(1))
            assert result["_source"] == "backoffice" and result["_source_confirmed"] is False
        else:
            with pytest.raises(SourceCheckDeferred):
                asyncio.run(client.get_release(1))

    if row is None:
        assert db.get(Release, 1) is None
    else:
        db.refresh(row)
        assert row.source == source and row.source_checked_at == CHECKED_AT
        assert row.is_hidden is False and row.is_blocked_by_copyrights is False


@pytest.mark.parametrize("scan", [False, True])
def test_first_unknown_geo_import_has_its_own_baseline_after_global_completion(db, scan):
    client = make_client(db)

    with mode(client, scan):
        result = asyncio.run(client.get_release(1))

    assert result["_notification_baseline"] is True
    assert get_setting_value(db, "backoffice_baseline_completed_at") == BASELINE_AT
    assert not get_setting_value(db, "backoffice_geo_baseline_completed_at")


@pytest.mark.parametrize("archive_baseline", [False, True])
def test_geo_initial_import_preserves_existing_archive_baseline_policy(db, archive_baseline):
    db.add(Release(release_id=1, source="site", is_hidden=False))
    db.commit()
    archive = add_archive(db, baseline=archive_baseline)
    client = make_client(db)

    result = asyncio.run(client.get_release(1))

    assert result.get("_notification_baseline", False) is archive_baseline
    db.refresh(archive)
    assert archive.notification_baseline is archive_baseline
    assert get_setting_value(db, "backoffice_baseline_completed_at") == BASELINE_AT


@pytest.mark.parametrize("scan", [False, True])
def test_new_avc_after_geo_baseline_completion_uses_ordinary_notifications(db, scan):
    db.add(Setting(key="backoffice_geo_baseline_completed_at", value=BASELINE_AT))
    db.commit()
    client = make_client(db, release_id=2)

    with mode(client, scan):
        result = asyncio.run(client.get_release(2))
        torrents = asyncio.run(client.get_torrents_for_release(2))

    assert "_notification_baseline" not in result
    assert torrents[0]["codec"]["label"] == "AVC"
    assert get_setting_value(db, "backoffice_baseline_completed_at") == BASELINE_AT


@pytest.mark.parametrize("scan", [False, True])
def test_geo_pipeline_retry_checks_exact_current_id_and_hash(db, scan):
    archive = add_archive(db)
    client = make_client(db)
    client.backoffice.get_torrents_for_release.return_value = [{"id": 10, "release_id": 1, "hash": HASH_B}]

    with mode(client, scan):
        assert not asyncio.run(client.pipeline_torrent_exists(SimpleNamespace(release_id=1, torrent_id=10, info_hash=HASH_A)))
        assert asyncio.run(client.pipeline_torrent_exists(SimpleNamespace(release_id=1, torrent_id=10, info_hash=HASH_B)))
        assert not asyncio.run(client.pipeline_torrent_exists(SimpleNamespace(release_id=1, torrent_id=11, info_hash=HASH_B)))
        assert asyncio.run(client.get_torrent(10))["hash"] == HASH_B
        assert asyncio.run(client.get_torrent(HASH_A)) is None

    db.refresh(archive)
    assert archive.info_hash == HASH_A and archive.api_present and not archive.superseded
