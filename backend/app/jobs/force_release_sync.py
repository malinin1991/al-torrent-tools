"""Принудительный sync одного релиза по URL / alias / id (без tracking)."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.db.models import JobLog
from app.jobs.meta_sync import _process_release_or_meta
from app.services.release_checkpoint import normalize_api_datetime
from app.services.runtime_settings import build_anilibria_client
from app.services.torrent_processor import TorrentProcessor
from app.utils.release_ref import ReleaseRef, ReleaseRefParseError, parse_release_ref


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def _extract_release_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("list", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _raw_release_input(params: dict[str, Any]) -> str | int | None:
    for key in ("release", "url", "alias", "release_alias"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return value
    release_id = params.get("release_id")
    if isinstance(release_id, int):
        return release_id
    if isinstance(release_id, str) and release_id.strip():
        return release_id.strip()
    return None


async def _resolve_release_from_api(
    al_client: Any,
    ref: ReleaseRef,
) -> tuple[int, str | None, str | None, str | None]:
    """Вернуть (release_id, alias, updated_at, fresh_at) или ValueError если не найден."""
    include = ["id", "alias", "updated_at", "fresh_at"]
    if ref.release_id is not None:
        payload = await al_client.get_releases_list(ids=[ref.release_id], include=include)
        items = _extract_release_list(payload)
        if not items:
            # Fallback: прямой get_release по id.
            try:
                data = await al_client.get_release(ref.release_id, include=include)
            except RuntimeError as exc:
                cause = exc.__cause__
                if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404:
                    raise ValueError(f"Релиз не найден: id={ref.release_id}") from exc
                raise
            if isinstance(data, dict) and isinstance(data.get("id"), int):
                items = [data]
        if not items:
            raise ValueError(f"Релиз не найден: id={ref.release_id}")
        item = items[0]
    else:
        alias = str(ref.alias or "").strip()
        payload = await al_client.get_releases_list(aliases=[alias], include=include)
        items = _extract_release_list(payload)
        if not items:
            try:
                data = await al_client.get_release(alias, include=include)
            except RuntimeError as exc:
                cause = exc.__cause__
                if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404:
                    raise ValueError(f"Релиз не найден: alias={alias}") from exc
                raise
            if isinstance(data, dict) and isinstance(data.get("id"), int):
                items = [data]
        if not items:
            raise ValueError(f"Релиз не найден: alias={alias}")
        item = items[0]

    release_id = item.get("id")
    if not isinstance(release_id, int):
        raise ValueError("AniLibria API вернул релиз без числового id")
    api_alias = item.get("alias") if isinstance(item.get("alias"), str) else None
    if not api_alias and ref.alias:
        api_alias = ref.alias
    updated_at = normalize_api_datetime(item.get("updated_at"))
    fresh_at = normalize_api_datetime(item.get("fresh_at"))
    return release_id, api_alias, updated_at, fresh_at


async def run_force_release_sync(db: Session, job_id: int, params: dict[str, Any]) -> None:
    """Принудительный process_release по URL/alias/id — tracking не требуется."""
    raw = _raw_release_input(params or {})
    try:
        ref = parse_release_ref(raw)
    except ReleaseRefParseError as exc:
        raise ValueError(str(exc)) from exc

    _add_log(
        db,
        job_id,
        "force_release_sync: разбор → "
        + (
            f"release_id={ref.release_id}"
            if ref.release_id is not None
            else f"alias={ref.alias}"
        ),
    )

    al_client = build_anilibria_client(db)
    release_id, alias, updated_at, fresh_at = await _resolve_release_from_api(al_client, ref)
    _add_log(
        db,
        job_id,
        f"force_release_sync: найден релиз id={release_id}, alias={alias or '-'}",
    )

    processor = TorrentProcessor(db=db, job_id=job_id, client=al_client)
    part = await _process_release_or_meta(
        processor,
        release_id=release_id,
        release_alias=alias,
        list_updated_at=updated_at,
        list_fresh_at=fresh_at,
        torrent_id_filter=None,
    )
    _add_log(
        db,
        job_id,
        TorrentProcessor.format_batch_summary(
            "force_release_sync: готово", part, releases=1
        ),
    )
