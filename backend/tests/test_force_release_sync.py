"""Тесты parse_release_ref и force_release_sync."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.jobs import force_release_sync as force_mod
from app.services.torrent_processor import TorrentProcessor
from app.utils.release_ref import ReleaseRef, ReleaseRefParseError, parse_release_ref


@pytest.mark.parametrize(
    ("raw", "expected_id", "expected_alias"),
    [
        (
            "https://aniliberty.top/anime/releases/release/re-creators",
            None,
            "re-creators",
        ),
        (
            "https://aniliberty.top/anime/releases/release/re-creators/torrents",
            None,
            "re-creators",
        ),
        (
            "https://aniliberty.top/anime/releases/release/re-creators/torrent?x=1",
            None,
            "re-creators",
        ),
        (
            "https://www.anilibria.tv/release/reiwa-no-dara-san.html",
            None,
            "reiwa-no-dara-san",
        ),
        ("3993", 3993, None),
        (3993, 3993, None),
        ("re-creators", None, "re-creators"),
        ("Reiwa-No-Dara-San", None, "reiwa-no-dara-san"),
        ("reiwa-no-dara-san.html", None, "reiwa-no-dara-san"),
    ],
)
def test_parse_release_ref_ok(raw, expected_id, expected_alias) -> None:
    ref = parse_release_ref(raw)
    assert ref.release_id == expected_id
    assert ref.alias == expected_alias


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://aniliberty.top/anime/releases",
        "https://example.com/foo/bar",
        "---",
        "123-456",
        "0",
        0,
        -1,
    ],
)
def test_parse_release_ref_errors(raw) -> None:
    with pytest.raises(ReleaseRefParseError):
        parse_release_ref(raw)


def test_release_ref_requires_exactly_one() -> None:
    with pytest.raises(ValueError):
        ReleaseRef()
    with pytest.raises(ValueError):
        ReleaseRef(release_id=1, alias="x")


def test_force_release_sync_requires_input() -> None:
    with pytest.raises(ValueError, match="URL|alias|id"):
        asyncio.run(force_mod.run_force_release_sync(MagicMock(), job_id=1, params={}))


def test_force_release_sync_bad_url() -> None:
    with pytest.raises(ValueError, match="не удалось разобрать|не содержит"):
        asyncio.run(
            force_mod.run_force_release_sync(
                MagicMock(),
                job_id=1,
                params={"release": "https://example.com/nope"},
            )
        )


def test_force_release_sync_by_alias_calls_process(monkeypatch) -> None:
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

    async def fake_process(_processor, **kwargs):
        called["kwargs"] = kwargs
        return {**TorrentProcessor.empty_release_stats(), "renames": 1}

    al = MagicMock()
    al.get_releases_list = AsyncMock(
        return_value=[{"id": 42, "alias": "re-creators", "updated_at": None, "fresh_at": None}]
    )

    monkeypatch.setattr(force_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(force_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(force_mod, "build_anilibria_client", lambda _db: al)
    monkeypatch.setattr(force_mod, "_process_release_or_meta", fake_process)
    monkeypatch.setattr(force_mod, "normalize_api_datetime", lambda v: v)

    asyncio.run(
        force_mod.run_force_release_sync(
            MagicMock(),
            job_id=1,
            params={"release": "https://aniliberty.top/anime/releases/release/re-creators"},
        )
    )

    al.get_releases_list.assert_awaited()
    assert called["kwargs"]["release_id"] == 42
    assert called["kwargs"]["release_alias"] == "re-creators"


def test_force_release_sync_by_id(monkeypatch) -> None:
    called: dict = {}

    class FakeProcessor:
        empty_release_stats = staticmethod(TorrentProcessor.empty_release_stats)
        format_batch_summary = staticmethod(TorrentProcessor.format_batch_summary)

        def __init__(self, **kwargs):
            pass

    async def fake_process(_processor, **kwargs):
        called["kwargs"] = kwargs
        return TorrentProcessor.empty_release_stats()

    al = MagicMock()
    al.get_releases_list = AsyncMock(return_value=[{"id": 3993, "alias": "foo"}])

    monkeypatch.setattr(force_mod, "TorrentProcessor", FakeProcessor)
    monkeypatch.setattr(force_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(force_mod, "build_anilibria_client", lambda _db: al)
    monkeypatch.setattr(force_mod, "_process_release_or_meta", fake_process)
    monkeypatch.setattr(force_mod, "normalize_api_datetime", lambda v: v)

    asyncio.run(
        force_mod.run_force_release_sync(
            MagicMock(), job_id=1, params={"release_id": 3993}
        )
    )

    assert called["kwargs"]["release_id"] == 3993


def test_force_release_sync_not_found(monkeypatch) -> None:
    al = MagicMock()
    al.get_releases_list = AsyncMock(return_value=[])
    request = httpx.Request("GET", "https://example.test/anime/releases/missing")
    response = httpx.Response(404, request=request)
    al.get_release = AsyncMock(
        side_effect=RuntimeError("AniLibria API недоступен")
    )
    # Attach 404 cause like the real client.
    err = RuntimeError("AniLibria API недоступен")
    err.__cause__ = httpx.HTTPStatusError("not found", request=request, response=response)
    al.get_release = AsyncMock(side_effect=err)

    monkeypatch.setattr(force_mod, "_add_log", lambda *a, **k: None)
    monkeypatch.setattr(force_mod, "build_anilibria_client", lambda _db: al)

    with pytest.raises(ValueError, match="не найден"):
        asyncio.run(
            force_mod.run_force_release_sync(
                MagicMock(), job_id=1, params={"alias": "missing-release"}
            )
        )


def test_job_catalog_includes_force_release_sync() -> None:
    from app.services.job_catalog import JOB_TYPE_DEFS

    entry = next(item for item in JOB_TYPE_DEFS if item.type == "force_release_sync")
    assert entry.manual_run is True
    assert entry.interval_default_sec is None
    assert "alias" in entry.description.lower() or "alias" in entry.manual_hint.lower()


def test_force_release_sync_job_action_validates(monkeypatch) -> None:
    from app.main import run_job_action

    db = MagicMock()
    request = MagicMock()

    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: MagicMock(context=ctx, name=name),
    )

    result = asyncio.run(
        run_job_action(
            request,
            job_type="force_release_sync",
            release="https://example.com/nope",
            db=db,
        )
    )
    assert "не удалось разобрать" in result.context["message"] or "не содержит" in result.context[
        "message"
    ]


def test_force_release_sync_job_action_runs(monkeypatch) -> None:
    from app.main import run_job_action

    db = MagicMock()
    request = MagicMock()
    job = MagicMock()
    job.id = 77
    job.type = "force_release_sync"

    create = MagicMock(return_value=job)
    schedule = MagicMock()
    monkeypatch.setattr("app.main.job_runner.create_job", create)
    monkeypatch.setattr("app.main.job_runner.schedule_job", schedule)
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: MagicMock(context=ctx, name=name),
    )

    result = asyncio.run(
        run_job_action(
            request,
            job_type="force_release_sync",
            release="re-creators",
            db=db,
        )
    )
    create.assert_called_once_with(db, "force_release_sync", {"release": "re-creators"})
    schedule.assert_called_once_with(77)
    assert "77" in result.context["message"]
