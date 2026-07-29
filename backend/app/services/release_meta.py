"""Upsert карточки релиза (жанры, состав, блокировки) в таблицы releases / release_members."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Release, ReleaseMember
from app.services.torrent_qb_meta import (
    extract_release_genres,
    extract_release_members,
    extract_release_names,
    parse_api_bool,
)
from app.utils.datetime_fmt import utcnow


@dataclass(frozen=True)
class ReleaseMetaView:
    """Снимок метаданных релиза для UI / фильтров."""

    release_id: int
    release_alias: str | None = None
    title: str | None = None
    genres: list[str] = field(default_factory=list)
    members: list[dict[str, str]] = field(default_factory=list)
    # None = ключ ещё не писали из API — UI может взять fallback из quality_json.
    is_blocked_by_geo: bool | None = None
    is_blocked_by_copyrights: bool | None = None


def _member_rows_from_dicts(release_id: int, members: list[dict[str, str]]) -> list[ReleaseMember]:
    rows: list[ReleaseMember] = []
    for idx, item in enumerate(members):
        nickname = (item.get("nickname") or "").strip()
        if not nickname:
            continue
        role = (item.get("role") or "unknown").strip().casefold() or "unknown"
        role_label = (item.get("role_label") or role).strip() or role
        api_id = (item.get("api_id") or "").strip() or None
        rows.append(
            ReleaseMember(
                release_id=release_id,
                api_member_id=api_id,
                role=role,
                role_label=role_label,
                nickname=nickname,
                sort_order=idx,
            )
        )
    return rows


def upsert_release_meta(
    db: Session,
    release_id: int,
    release_payload: dict[str, Any],
    *,
    commit: bool = True,
) -> Release:
    """Создаёт/обновляет ``releases`` + ``release_members`` из payload get_release.

    Sparse include: пишем только присутствующие ключи (не затираем соседей).
    ``members: []`` при ключе-списке в payload очищает состав;
    ``members: null`` / не-list — пропускаем (не затираем).
    """
    rid = int(release_id)
    row = db.get(Release, rid)
    if row is None:
        row = Release(release_id=rid)
        db.add(row)
        db.flush()

    alias = release_payload.get("alias")
    if isinstance(alias, str) and alias.strip():
        row.release_alias = alias.strip()

    main, _english = extract_release_names(release_payload)
    if main:
        row.title = main

    if "genres" in release_payload:
        genres = extract_release_genres(release_payload)
        row.genres_json = list(genres)

    if "is_blocked_by_geo" in release_payload or "is_blocked_by_copyrights" in release_payload:
        if "is_blocked_by_geo" in release_payload:
            parsed = parse_api_bool(release_payload.get("is_blocked_by_geo"))
            row.is_blocked_by_geo = False if parsed is None else parsed
        if "is_blocked_by_copyrights" in release_payload:
            parsed = parse_api_bool(release_payload.get("is_blocked_by_copyrights"))
            row.is_blocked_by_copyrights = False if parsed is None else parsed

    if "members" in release_payload:
        raw_members = release_payload.get("members")
        # Только list (в т.ч. []) обновляет состав; null/битый тип — не затираем.
        if isinstance(raw_members, list):
            members = extract_release_members(release_payload)
            db.execute(delete(ReleaseMember).where(ReleaseMember.release_id == rid))
            for member in _member_rows_from_dicts(rid, members):
                db.add(member)

    row.updated_at = utcnow()
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def load_release_meta_by_ids(
    db: Session, release_ids: list[int]
) -> dict[int, ReleaseMetaView]:
    """Карта release_id → метаданные; пустой список → {}."""
    ids = [int(x) for x in release_ids if x is not None]
    if not ids:
        return {}
    rows = db.scalars(
        select(Release)
        .where(Release.release_id.in_(ids))
        .options(selectinload(Release.members))
    ).all()
    out: dict[int, ReleaseMetaView] = {}
    for row in rows:
        members = [
            {
                "role": m.role,
                "role_label": m.role_label,
                "nickname": m.nickname,
                **({"api_id": m.api_member_id} if m.api_member_id else {}),
            }
            for m in sorted(row.members, key=lambda x: (x.sort_order, x.id))
        ]
        genres = row.genres_json if isinstance(row.genres_json, list) else []
        out[int(row.release_id)] = ReleaseMetaView(
            release_id=int(row.release_id),
            release_alias=row.release_alias,
            title=row.title,
            genres=[str(g) for g in genres if isinstance(g, str) and g.strip()],
            members=members,
            is_blocked_by_geo=row.is_blocked_by_geo,
            is_blocked_by_copyrights=row.is_blocked_by_copyrights,
        )
    return out
