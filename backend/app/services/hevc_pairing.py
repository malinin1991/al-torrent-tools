"""Пары AVC↔HEVC внутри релиза для фильтров /releases.

missing         — HEVC вообще нет для слота (release + batch_start + quality /
                  source class). WEBRip↔WEB-DL не блокирует «HEVC есть».
                  AVC новее HEVC → не missing (это overdue после SLA).
overdue         — на слоте уже есть HEVC (тот же batch_start; старый/частичный
                  ОК) И age > 24h без актуального exact-аналога
                  (тот же rip type+quality+episodes), либо AVC новее exact-HEVC
                  по AniLibria torrent_id (created_at ALTT — только tie-break).
                  Pure missing (нет HEVC на batch_start) никогда не overdue.
type_mismatch   — HEVC есть (тот же start+quality в web-классе), но тип рипа
                  WEBRip↔WEB-DL(WEBDL) расходится. Не попадаёт в missing.

Бейдж (status): overdue > type_mismatch > missing.
Число на бейдже «просрочка Nч» — часы сверх SLA: max(0, age − 24), не полный age.
Фильтры независимы: один AVC может быть overdue и type_mismatch сразу;
overdue и missing взаимоисключающи (overdue ⇒ has_hevc_for_batch_start).
ignore_hevc на архиве AVC закрывает missing/overdue/type_mismatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.telegram_notify import classify_torrent_codec_family
from app.utils.datetime_fmt import utcnow

HEVC_SLA_HOURS = 24

HevcFilter = Literal["", "missing", "overdue", "type_mismatch"]
HevcPairStatus = Literal["missing", "overdue", "type_mismatch"]
HevcNeedState = Literal["ok", "missing", "overdue", "type_mismatch"]

# Кодек в torrent_type / label — вырезаем для ключа семейства рипа.
_CODEC_TOKEN_RE = re.compile(
    r"\b(?:AVC|HEVC|AV1|x264|x265|h\.?264|h\.?265)\b",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_WEBRIP_RE = re.compile(r"^webrip$", re.IGNORECASE)
_WEBDL_RE = re.compile(r"^web-?dl$", re.IGNORECASE)

# Эпизодный старт батча: regular / ova / film.
# Все film-like ярлыки → один ключ ("film",) для presence/missing.
# overdue по-прежнему требует точный torrent_description (exact_pair_key).
_FILM_RE = re.compile(
    r"^(?:"
    r"фильм|film|"
    r"п\s*/\s*ф(?:\s+фильм)?|"
    r"полнометражный(?:\s+фильм)?"
    r")$",
    re.IGNORECASE,
)
_OVA_ALONE_RE = re.compile(r"^ova$", re.IGNORECASE)
_OVA_RANGE_RE = re.compile(r"^ova\s+(\d+)(?:\s*-\s*\d+)?$", re.IGNORECASE)
_REGULAR_RE = re.compile(r"^(\d+)(?:\s*-\s*\d+)?$")

# Неполный ключ: не кладём в множества HEVC и не считаем «есть пара».
BatchStartKey = tuple[Any, ...]
# (quality, source_class, batch_start) — presence для missing.
PresenceKey = tuple[str, str, BatchStartKey]


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


def normalize_rip_type(raw: str | None) -> str:
    """Канон типа рипа: WEBDL/WEB-DL → WEB-DL; WEBRip → WEBRip; иначе as-is."""
    text = (raw or "").strip()
    if not text:
        return ""
    if _WEBRIP_RE.match(text):
        return "WEBRip"
    if _WEBDL_RE.match(text):
        return "WEB-DL"
    return text


def is_web_rip_type(rip_type: str | None) -> bool:
    return normalize_rip_type(rip_type) in ("WEBRip", "WEB-DL")


def source_class_for_rip(rip_type: str | None) -> str:
    """Класс источника для presence/missing: web | конкретный тип (BDRip…)."""
    normalized = normalize_rip_type(rip_type)
    if not normalized:
        return ""
    if is_web_rip_type(normalized):
        return "web"
    return normalized


def _type_and_quality_parts(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
) -> tuple[str, str]:
    """(rip_type, quality) без кодека; пустые строки если не удалось разобрать."""
    qj = quality_json if isinstance(quality_json, dict) else {}
    type_part = _field_text(qj.get("type"))
    quality_part = _field_text(qj.get("quality"))
    if type_part or quality_part:
        type_clean = _strip_codec_tokens(type_part or "")
        quality_clean = _strip_codec_tokens(quality_part or "")
        # Иногда type тащит «WEBRip 1080p» — отделим хвост quality.
        if type_clean and not quality_clean:
            tokens = type_clean.split()
            if len(tokens) >= 2 and tokens[-1].lower().endswith("p"):
                return normalize_rip_type(tokens[0]), tokens[-1]
        return normalize_rip_type(type_clean), quality_clean

    raw = _strip_codec_tokens((torrent_type or "").strip())
    if not raw:
        return "", ""
    tokens = raw.split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return normalize_rip_type(tokens[0]), ""
    # «WEBRip 1080p» / «WEB-DL 1080p» / «BDRip 1080p»
    quality = tokens[-1] if tokens[-1].lower().endswith("p") else ""
    type_tokens = tokens[:-1] if quality else tokens
    return normalize_rip_type(" ".join(type_tokens)), quality


def rip_type_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
) -> str:
    return _type_and_quality_parts(quality_json=quality_json, torrent_type=torrent_type)[0]


def quality_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
) -> str:
    return _type_and_quality_parts(quality_json=quality_json, torrent_type=torrent_type)[1]


def rip_family_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
) -> str:
    """Семейство рипа: type + quality без кодека (WEBRip и WEB-DL различны)."""
    rip_type, quality = _type_and_quality_parts(
        quality_json=quality_json, torrent_type=torrent_type
    )
    return _WS_RE.sub(" ", " ".join(part for part in (rip_type, quality) if part)).strip()


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


def presence_pair_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
    torrent_description: str | None,
) -> PresenceKey | None:
    """Ключ presence для missing: quality + source_class + batch_start."""
    quality = quality_key(quality_json=quality_json, torrent_type=torrent_type)
    rip_type = rip_type_key(quality_json=quality_json, torrent_type=torrent_type)
    source = source_class_for_rip(rip_type)
    start = batch_start_key(torrent_description)
    if not quality or not source or start is None:
        return None
    return (quality, source, start)


def start_pair_key(
    *,
    quality_json: dict[str, Any] | None,
    torrent_type: str | None,
    torrent_description: str | None,
) -> tuple[str, BatchStartKey] | None:
    """Обратная совместимость: (rip_family, batch_start); None если неполный."""
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


def overdue_hours_past_sla(
    age: float | None, *, sla_hours: float = HEVC_SLA_HOURS
) -> float | None:
    """Часы сверх SLA для бейджа; фильтр overdue по-прежнему смотрит на полный age."""
    if age is None:
        return None
    return max(0.0, float(age) - float(sla_hours))


def _created_is_newer(left: datetime | None, right: datetime | None) -> bool:
    """True если left строго новее right; None не сравниваем → False."""
    if left is None or right is None:
        return False
    return _as_naive_utc(left) > _as_naive_utc(right)


def _avc_is_newer_than_hevc(
    *,
    avc_torrent_id: int,
    avc_created_at: datetime | None,
    hevc: _HevcPairRef,
) -> bool:
    """True если HEVC устарел относительно AVC (нужен catch-up).

    Источник истины порядка загрузок AniLibria — ``torrent_id``.
    ``created_at`` ALTT только tie-break при равных torrent_id (иначе ingest
    HEVC раньше AVC даёт вечный overdue при более новом hevc.torrent_id).
    """
    if avc_torrent_id and hevc.torrent_id:
        if hevc.torrent_id < avc_torrent_id:
            return True
        if hevc.torrent_id > avc_torrent_id:
            return False
    return _created_is_newer(avc_created_at, hevc.created_at)


@dataclass(frozen=True)
class _HevcPairRef:
    created_at: datetime | None
    info_hash: str | None
    torrent_id: int
    archive_id: int
    rip_type: str


def _pick_latest_hevc(current: _HevcPairRef | None, candidate: _HevcPairRef) -> _HevcPairRef:
    if current is None:
        return candidate
    # Порядок AniLibria: больший torrent_id = более поздняя загрузка.
    if candidate.torrent_id != current.torrent_id:
        return candidate if candidate.torrent_id > current.torrent_id else current
    if candidate.created_at is None:
        return current
    if current.created_at is None:
        return candidate
    if _as_naive_utc(candidate.created_at) >= _as_naive_utc(current.created_at):
        return candidate
    return current


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
    type_mismatch: bool = False
    hevc_outdated: bool = False
    paired_hevc_info_hash: str | None = None
    paired_hevc_torrent_id: int | None = None
    batch_start: BatchStartKey | None = None
    ignore_hevc: bool = False

    @property
    def status(self) -> HevcPairStatus:
        """Бейдж: overdue > type_mismatch > missing."""
        if self.overdue:
            return "overdue"
        if self.type_mismatch:
            return "type_mismatch"
        return "missing"

    @property
    def need_state(self) -> HevcNeedState:
        """Состояние для pipeline events — совпадает с бейджем status."""
        return self.status


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
    """AVC с проблемой HEVC: missing / overdue / type_mismatch."""
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
        hevc_presence: dict[PresenceKey, _HevcPairRef] = {}
        hevc_presence_types: dict[PresenceKey, set[str]] = {}
        hevc_exact: dict[tuple[str, str], _HevcPairRef] = {}
        avc_rows: list[Any] = []

        for row in rows:
            qj = _archive_attr(row, "quality_json")
            qj = qj if isinstance(qj, dict) else None
            torrent_type = _archive_attr(row, "torrent_type")
            desc = _archive_attr(row, "torrent_description")
            codec = classify_archive_codec(quality_json=qj, torrent_type=torrent_type)
            raw_hash = _archive_attr(row, "info_hash")
            info_hash_norm = (
                raw_hash.strip().lower() or None if isinstance(raw_hash, str) else None
            )
            rip_type = rip_type_key(quality_json=qj, torrent_type=torrent_type)
            ref = _HevcPairRef(
                created_at=_archive_attr(row, "created_at"),
                info_hash=info_hash_norm,
                torrent_id=int(_archive_attr(row, "torrent_id") or 0),
                archive_id=int(_archive_attr(row, "id")),
                rip_type=rip_type,
            )

            if codec == "HEVC":
                presence = presence_pair_key(
                    quality_json=qj,
                    torrent_type=torrent_type,
                    torrent_description=desc,
                )
                if presence is not None:
                    hevc_presence[presence] = _pick_latest_hevc(
                        hevc_presence.get(presence), ref
                    )
                    if rip_type:
                        hevc_presence_types.setdefault(presence, set()).add(rip_type)
                exact_key = exact_pair_key(
                    quality_json=qj,
                    torrent_type=torrent_type,
                    torrent_description=desc,
                )
                if exact_key is not None:
                    hevc_exact[exact_key] = _pick_latest_hevc(hevc_exact.get(exact_key), ref)
            elif codec == "AVC":
                avc_rows.append(row)

        for row in avc_rows:
            # Ручной «Игнорировать HEVC» — считаем пару закрытой для фильтров/бейджей.
            if bool(_archive_attr(row, "ignore_hevc", False)):
                continue
            qj = _archive_attr(row, "quality_json")
            qj = qj if isinstance(qj, dict) else None
            torrent_type = _archive_attr(row, "torrent_type")
            desc = _archive_attr(row, "torrent_description")
            family = rip_family_key(quality_json=qj, torrent_type=torrent_type)
            episodes = normalize_episodes(desc)
            created = _archive_attr(row, "created_at")
            hours = age_hours(created, now=current)
            avc_rip_type = rip_type_key(quality_json=qj, torrent_type=torrent_type)
            avc_torrent_id = int(_archive_attr(row, "torrent_id") or 0)

            presence = presence_pair_key(
                quality_json=qj,
                torrent_type=torrent_type,
                torrent_description=desc,
            )
            presence_ref = hevc_presence.get(presence) if presence is not None else None
            has_presence = presence_ref is not None
            # Неполный ключ / нет HEVC в слоте → missing. Устаревший HEVC — не missing.
            is_missing = presence is None or not has_presence

            types_at = hevc_presence_types.get(presence, set()) if presence else set()
            is_type_mismatch = (
                not is_missing
                and is_web_rip_type(avc_rip_type)
                and avc_rip_type not in types_at
                and any(is_web_rip_type(t) for t in types_at)
            )

            exact_key = exact_pair_key(
                quality_json=qj,
                torrent_type=torrent_type,
                torrent_description=desc,
            )
            exact_ref = hevc_exact.get(exact_key) if exact_key is not None else None
            has_exact = exact_ref is not None
            avc_newer_exact = has_exact and _avc_is_newer_than_hevc(
                avc_torrent_id=avc_torrent_id,
                avc_created_at=created,
                hevc=exact_ref,
            )
            needs_exact_catchup = (not has_exact) or avc_newer_exact
            # overdue только если HEVC уже был на этом batch_start (частичный/старый).
            # Pure missing (нет presence) — только missing, даже при age > SLA.
            is_overdue = (
                has_presence
                and needs_exact_catchup
                and hours is not None
                and hours > sla_hours
            )

            if not is_missing and not is_overdue and not is_type_mismatch:
                continue

            pair_ref = exact_ref or presence_ref
            unpaired.append(
                UnpairedAvc(
                    archive_id=int(_archive_attr(row, "id")),
                    release_id=release_id,
                    torrent_id=avc_torrent_id,
                    rip_family=family,
                    episodes=episodes,
                    created_at=created,
                    age_hours=hours,
                    missing=is_missing,
                    overdue=is_overdue,
                    type_mismatch=is_type_mismatch,
                    hevc_outdated=bool(avc_newer_exact),
                    paired_hevc_info_hash=pair_ref.info_hash if pair_ref else None,
                    paired_hevc_torrent_id=(
                        pair_ref.torrent_id if pair_ref and pair_ref.torrent_id else None
                    ),
                    batch_start=presence[2] if presence is not None else None,
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
    """release_id с ≥1 AVC под фильтр; missing / overdue / type_mismatch независимы."""
    if hevc_filter not in ("missing", "overdue", "type_mismatch"):
        return set()
    unmatched = find_unpaired_avc(archives, now=now, sla_hours=sla_hours)
    if hevc_filter == "overdue":
        return {item.release_id for item in unmatched if item.overdue}
    if hevc_filter == "type_mismatch":
        return {item.release_id for item in unmatched if item.type_mismatch}
    return {item.release_id for item in unmatched if item.missing}


def _need_message(state: HevcNeedState) -> str:
    if state == "ok":
        return "HEVC пара найдена"
    if state == "overdue":
        return "Просрочка HEVC"
    if state == "type_mismatch":
        return "Расхождение типов"
    return "Нет HEVC"


def _need_flags_fingerprint(
    *,
    missing: bool,
    overdue: bool,
    type_mismatch: bool,
    hevc_outdated: bool,
    paired_hevc_info_hash: str | None,
) -> tuple[bool, bool, bool, bool, str | None]:
    """Структурные флаги need-state (без age) — для детекта смены причины."""
    return (missing, overdue, type_mismatch, hevc_outdated, paired_hevc_info_hash)


def sync_hevc_pair_events_for_release(
    db: Session,
    release_id: int,
    *,
    job_id: int | None = None,
    now: datetime | None = None,
    sla_hours: float = HEVC_SLA_HOURS,
) -> int:
    """Пишет PipelineEvent hevc_status при смене need-state AVC пайплайнов релиза.

    Listing (/releases) не вызывает — только sync/discover пути с записью в БД.
    Возвращает число записанных событий.
    """
    from app.db.models import PipelineEvent, TorrentArchive, TorrentPipeline
    from app.services.pipeline import record_pipeline_event

    archives = list(
        db.scalars(
            select(TorrentArchive).where(
                TorrentArchive.release_id == release_id,
                TorrentArchive.api_present.is_(True),
                TorrentArchive.superseded.is_(False),
            )
        ).all()
    )
    if not archives:
        return 0

    unpaired = unpaired_by_archive_id(archives, now=now, sla_hours=sla_hours)
    avc_archives: list[Any] = []
    for row in archives:
        qj = row.quality_json if isinstance(row.quality_json, dict) else None
        codec = classify_archive_codec(quality_json=qj, torrent_type=row.torrent_type)
        if codec == "AVC":
            avc_archives.append(row)

    emitted = 0
    for row in avc_archives:
        info_hash = (row.info_hash or "").strip().lower()
        if not info_hash:
            continue
        pipeline = db.scalar(
            select(TorrentPipeline)
            .where(
                TorrentPipeline.info_hash == info_hash,
                TorrentPipeline.status.not_in(("failed", "cancelled")),
            )
            .order_by(TorrentPipeline.id.desc())
            .limit(1)
        )
        if pipeline is None:
            pipeline = db.scalar(
                select(TorrentPipeline)
                .where(TorrentPipeline.info_hash == info_hash)
                .order_by(TorrentPipeline.id.desc())
                .limit(1)
            )
        if pipeline is None:
            continue

        item = unpaired.get(int(row.id))
        ignored = bool(getattr(row, "ignore_hevc", False))
        new_state: HevcNeedState = (
            "ok" if ignored else (item.need_state if item is not None else "ok")
        )

        last = db.scalar(
            select(PipelineEvent)
            .where(
                PipelineEvent.pipeline_id == pipeline.id,
                PipelineEvent.event_type == "hevc_status",
            )
            .order_by(PipelineEvent.id.desc())
            .limit(1)
        )
        prev_state = (last.to_status if last is not None else None) or "ok"
        if not isinstance(prev_state, str):
            prev_state = "ok"

        new_flags = _need_flags_fingerprint(
            missing=bool(item.missing) if item and not ignored else False,
            overdue=bool(item.overdue) if item and not ignored else False,
            type_mismatch=bool(item.type_mismatch) if item and not ignored else False,
            hevc_outdated=bool(item.hevc_outdated) if item and not ignored else False,
            paired_hevc_info_hash=(
                None
                if ignored
                else (item.paired_hevc_info_hash if item else None)
            ),
        )
        raw_prev_details = getattr(last, "details_json", None) if last is not None else None
        prev_details = raw_prev_details if isinstance(raw_prev_details, dict) else {}
        prev_flags = _need_flags_fingerprint(
            missing=bool(prev_details.get("missing")),
            overdue=bool(prev_details.get("overdue")),
            type_mismatch=bool(prev_details.get("type_mismatch")),
            hevc_outdated=bool(prev_details.get("hevc_outdated")),
            paired_hevc_info_hash=(
                prev_details.get("paired_hevc_info_hash")
                if isinstance(prev_details.get("paired_hevc_info_hash"), str)
                else None
            ),
        )
        prev_ignored = bool(prev_details.get("ignore_hevc"))
        # Переход need-state или смена причины при том же бейдже
        # (overdue+missing → overdue+type_mismatch). Без details у прошлого
        # события не сравниваем флаги — иначе legacy/mock спамили бы каждый sync.
        if (
            prev_state == new_state
            and prev_ignored == ignored
            and (not prev_details or prev_flags == new_flags)
        ):
            continue

        presence = presence_pair_key(
            quality_json=row.quality_json if isinstance(row.quality_json, dict) else None,
            torrent_type=row.torrent_type,
            torrent_description=row.torrent_description,
        )
        if ignored:
            reason = "ignore_hevc"
        elif new_state != "ok":
            reason = new_state
        else:
            reason = "paired"
        details: dict[str, Any] = {
            "actor": "job" if job_id is not None else "pipeline",
            "release_id": release_id,
            "torrent_id": int(row.torrent_id or 0),
            "info_hash": info_hash,
            "rip_family": (
                item.rip_family
                if item is not None
                else rip_family_key(
                    quality_json=row.quality_json if isinstance(row.quality_json, dict) else None,
                    torrent_type=row.torrent_type,
                )
            ),
            "batch_start_key": list(presence[2]) if presence is not None else None,
            "episodes": normalize_episodes(row.torrent_description),
            "reason": reason,
            "paired_hevc_info_hash": None if ignored else (item.paired_hevc_info_hash if item else None),
            "paired_hevc_torrent_id": (
                None if ignored else (item.paired_hevc_torrent_id if item else None)
            ),
            "hevc_outdated": bool(item.hevc_outdated) if item and not ignored else False,
            "missing": bool(item.missing) if item and not ignored else False,
            "overdue": bool(item.overdue) if item and not ignored else False,
            "type_mismatch": bool(item.type_mismatch) if item and not ignored else False,
            "ignore_hevc": ignored,
        }
        message = "Игнор HEVC" if ignored else _need_message(new_state)
        record_pipeline_event(
            db,
            pipeline.id,
            event_type="hevc_status",
            message=message,
            job_id=job_id,
            from_status=prev_state if last is not None else None,
            to_status=new_state,
            details=details,
            commit=True,
        )
        emitted += 1
    return emitted
