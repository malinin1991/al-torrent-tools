"""Пары AVC↔HEVC внутри релиза для фильтров /releases.

missing  — нет HEVC с тем же rip_family и batch_start_key (старый короткий диапазон ок).
overdue  — age > 24h и нет HEVC с точным тем же torrent_description.
Фильтры независимы: AVC может быть overdue, но не missing (есть старый HEVC).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from app.services.telegram_notify import classify_torrent_codec_family
from app.utils.datetime_fmt import utcnow

HEVC_SLA_HOURS = 24

HevcFilter = Literal["", "missing", "overdue"]
HevcPairStatus = Literal["missing", "overdue"]

# Кодек в torrent_type / label — вырезаем для ключа семейства рипа.
_CODEC_TOKEN_RE = re.compile(
    r"\b(?:AVC|HEVC|AV1|x264|x265|h\.?264|h\.?265)\b",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")

# Эпизодный старт батча: regular / ova / film.
_FILM_RE = re.compile(r"^(?:фильм|film)$", re.IGNORECASE)
_OVA_ALONE_RE = re.compile(r"^ova$", re.IGNORECASE)
_OVA_RANGE_RE = re.compile(r"^ova\s+(\d+)(?:\s*-\s*\d+)?$", re.IGNORECASE)
_REGULAR_RE = re.compile(r"^(\d+)(?:\s*-\s*\d+)?$")

# Неполный ключ: не кладём в множества HEVC и не считаем «есть пара».
BatchStartKey = tuple[Any, ...]


def _field_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("value", "description", "label"):
            text = _field_text(value.get(key))
            if text:
                return text
    return None


def normalize_episodes(description: str | None) -> str:
    return (description or "").strip()


def batch_start_key(description: str | None) -> BatchStartKey | None:
    """Ключ старта батча эпизодов; None — неразобранное / пустое (не пара)."""
    text = normalize_episodes(description)
    if not text:
        return None
    folded = text.casefold()

    if _FILM_RE.match(folded):
        return ("film",)

    if _OVA_ALONE_RE.match(folded):
        return ("ova", 1)

    ova_m = _OVA_RANGE_RE.match(folded)
    if ova_m:
        return ("ova", int(ova_m.group(1)))

    regular_m = _REGULAR_RE.match(folded)
    if regular_m:
        return ("regular", int(regular_m.group(1)))

    return None


def _strip_codec_tokens(raw: str) -> str:
    cleaned = _CODEC_TOKEN_RE.sub(" ", raw)
    return _WS_RE.sub(" ", cleaned).strip()


def rip_family_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
) -> str:
    """Семейство рипа: type + quality без кодека (например «BDRip 1080p»)."""
    qj = quality_json if isinstance(quality_json, dict) else {}
    type_part = _field_text(qj.get("type"))
    quality_part = _field_text(qj.get("quality"))
    if type_part or quality_part:
        raw = " ".join(part for part in (type_part, quality_part) if part)
        return _strip_codec_tokens(raw)

    raw = (torrent_type or "").strip()
    if not raw:
        return ""
    return _strip_codec_tokens(raw)


def classify_archive_codec(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None = None,
) -> str | None:
    """AVC / HEVC / AV1 по quality_json и fallback torrent_type."""
    payload: dict[str, Any] = {}
    qj = quality_json if isinstance(quality_json, dict) else {}
    for key in ("codec", "type", "label"):
        if key in qj:
            payload[key] = qj[key]
    family = classify_torrent_codec_family(payload) if payload else None
    if family:
        return family
    if torrent_type and torrent_type.strip():
        return classify_torrent_codec_family({"label": torrent_type})
    return None


def exact_pair_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
    torrent_description: str | None,
) -> tuple[str, str] | None:
    """Ключ точной пары для overdue; None если неполный (нельзя матчить)."""
    family = rip_family_key(quality_json=quality_json, torrent_type=torrent_type)
    episodes = normalize_episodes(torrent_description)
    if not family or not episodes:
        return None
    return (family, episodes)


def start_pair_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
    torrent_description: str | None,
) -> tuple[str, BatchStartKey] | None:
    """Ключ start-пары для missing; None если неполный."""
    family = rip_family_key(quality_json=quality_json, torrent_type=torrent_type)
    start = batch_start_key(torrent_description)
    if not family or start is None:
        return None
    return (family, start)


# Обратная совместимость: точный ключ (может быть неполным — пустые поля).
def pair_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
    torrent_description: str | None,
) -> tuple[str, str]:
    return (
        rip_family_key(quality_json=quality_json, torrent_type=torrent_type),
        normalize_episodes(torrent_description),
    )


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def age_hours(created_at: datetime | None, *, now: datetime | None = None) -> float | None:
    if created_at is None:
        return None
    current = _as_naive_utc(now or utcnow())
    return (current - _as_naive_utc(created_at)).total_seconds() / 3600.0


@dataclass(frozen=True)
class UnpairedAvc:
    archive_id: int
    release_id: int
    torrent_id: int
    rip_family: str
    episodes: str
    created_at: datetime | None
    age_hours: float | None
    missing: bool
    overdue: bool

    @property
    def status(self) -> HevcPairStatus:
        """Бейдж: overdue приоритетнее missing."""
        return "overdue" if self.overdue else "missing"


def _archive_attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def find_unpaired_avc(
    archives: Sequence[Any],
    *,
    now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS,
    require_active: bool = True,
) -> list[UnpairedAvc]:
    """AVC с проблемой HEVC: missing (start-key) и/или overdue (exact + SLA)."""
    current = now or utcnow()
    by_release: dict[int, list[Any]] = {}
    for row in archives:
        if require_active:
            if not bool(_archive_attr(row, "api_present", True)):
                continue
            if bool(_archive_attr(row, "superseded", False)):
                continue
        release_id = int(_archive_attr(row, "release_id"))
        by_release.setdefault(release_id, []).append(row)

    unpaired: list[UnpairedAvc] = []
    for release_id, rows in by_release.items():
        hevc_start: set[tuple[str, BatchStartKey]] = set()
        hevc_exact: set[tuple[str, str]] = set()
        avc_rows: list[Any] = []

        for row in rows:
            qj = _archive_attr(row, "quality_json")
            qj = qj if isinstance(qj, dict) else None
            torrent_type = _archive_attr(row, "torrent_type")
            desc = _archive_attr(row, "torrent_description")
            codec = classify_archive_codec(quality_json=qj, torrent_type=torrent_type)

            if codec == "HEVC":
                start_key = start_pair_key(
                    quality_json=qj,
                    torrent_type=torrent_type,
                    torrent_description=desc,
                )
                if start_key is not None:
                    hevc_start.add(start_key)
                exact_key = exact_pair_key(
                    quality_json=qj,
                    torrent_type=torrent_type,
                    torrent_description=desc,
                )
                if exact_key is not None:
                    hevc_exact.add(exact_key)
            elif codec == "AVC":
                avc_rows.append(row)

        for row in avc_rows:
            qj = _archive_attr(row, "quality_json")
            qj = qj if isinstance(qj, dict) else None
            torrent_type = _archive_attr(row, "torrent_type")
            desc = _archive_attr(row, "torrent_description")
            family = rip_family_key(quality_json=qj, torrent_type=torrent_type)
            episodes = normalize_episodes(desc)

            start_key = start_pair_key(
                quality_json=qj,
                torrent_type=torrent_type,
                torrent_description=desc,
            )
            # Неполный ключ → нет валидной пары → missing.
            is_missing = start_key is None or start_key not in hevc_start

            exact_key = exact_pair_key(
                quality_json=qj,
                torrent_type=torrent_type,
                torrent_description=desc,
            )
            has_exact = exact_key is not None and exact_key in hevc_exact

            created = _archive_attr(row, "created_at")
            hours = age_hours(created, now=current)
            is_overdue = (not has_exact) and hours is not None and hours > sla_hours

            if not is_missing and not is_overdue:
                continue

            unpaired.append(
                UnpairedAvc(
                    archive_id=int(_archive_attr(row, "id")),
                    release_id=release_id,
                    torrent_id=int(_archive_attr(row, "torrent_id") or 0),
                    rip_family=family,
                    episodes=episodes,
                    created_at=created,
                    age_hours=hours,
                    missing=is_missing,
                    overdue=is_overdue,
                )
            )
    return unpaired


def unpaired_by_archive_id(
    archives: Sequence[Any],
    *,
    now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS,
) -> dict[int, UnpairedAvc]:
    return {
        item.archive_id: item
        for item in find_unpaired_avc(archives, now=now, sla_hours=sla_hours)
    }


def release_ids_matching_hevc_filter(
    archives: Sequence[Any],
    *,
    hevc_filter: HevcFilter,
    now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS,
) -> set[int]:
    """release_id с ≥1 AVC под фильтр; missing и overdue независимы."""
    if hevc_filter not in ("missing", "overdue"):
        return set()
    unmatched = find_unpaired_avc(archives, now=now, sla_hours=sla_hours)
    if hevc_filter == "overdue":
        return {item.release_id for item in unmatched if item.overdue}
    return {item.release_id for item in unmatched if item.missing}
