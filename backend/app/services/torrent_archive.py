import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import TorrentArchive
from app.services.qbittorrent import sanitize_info_hash
from app.utils.datetime_fmt import as_utc_iso


def resolve_torrent_storage_root() -> Path:
    """Корень архива: TORRENT_STORAGE_DIR → settings.torrent_storage_dir (единый канон)."""
    env_dir = os.environ.get("TORRENT_STORAGE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    from app.core.config import default_torrent_storage_dir, settings

    configured = (settings.torrent_storage_dir or "").strip()
    if configured:
        return Path(configured)
    return Path(default_torrent_storage_dir())


class TorrentArchiveService:
    def __init__(self, db: Session, storage_root: Path | None = None) -> None:
        self._db = db
        self._storage_root = Path(storage_root) if storage_root is not None else resolve_torrent_storage_root()

    @staticmethod
    def _extract_anime_name(release_payload: dict[str, Any]) -> str | None:
        name = release_payload.get("name")
        if not isinstance(name, dict):
            return None
        for key in ("main", "english", "alternative"):
            value = name.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _build_category(release_payload: dict[str, Any]) -> str | None:
        """Категория qBittorrent: AniLibria/{год}, например AniLibria/2026."""
        year_text = ""
        year_value = release_payload.get("year")
        if isinstance(year_value, int):
            year_text = str(year_value)
        elif isinstance(year_value, str) and year_value.strip().isdigit():
            year_text = year_value.strip()

        if not year_text:
            season = release_payload.get("season")
            if isinstance(season, dict):
                for key in ("year", "value"):
                    raw = season.get(key)
                    if isinstance(raw, int):
                        year_text = str(raw)
                        break
                    if isinstance(raw, str) and raw.strip().isdigit():
                        year_text = raw.strip()
                        break

        if not year_text:
            return "AniLibria"
        return f"AniLibria/{year_text}"

    @staticmethod
    def _build_quality_json(torrent_payload: dict[str, Any]) -> dict[str, Any]:
        quality_json: dict[str, Any] = {}
        for key in ("quality", "type", "codec", "color"):
            value = torrent_payload.get(key)
            if isinstance(value, dict) and value:
                quality_json[key] = value

        for key in ("label", "bitrate", "is_hardsub", "seeders", "leechers", "sort_order"):
            value = torrent_payload.get(key)
            if isinstance(value, (str, int, bool)):
                quality_json[key] = value
        return quality_json

    @staticmethod
    def _attach_release_names(
        quality_json: dict[str, Any],
        release_payload: dict[str, Any],
    ) -> dict[str, Any]:
        from app.services.torrent_qb_meta import (
            extract_release_genres,
            extract_release_members,
            extract_release_names,
        )

        main, original = extract_release_names(release_payload)
        if main or original:
            quality_json = {**quality_json, "names": {"main": main, "english": original}}
        genres = extract_release_genres(release_payload)
        if genres:
            quality_json = {**quality_json, "genres": genres}
        members = extract_release_members(release_payload)
        if members:
            quality_json = {**quality_json, "members": members}
        # Каждый флаг — только если ключ есть (не сбрасывать соседний при sparse payload).
        if "is_blocked_by_geo" in release_payload:
            quality_json = {
                **quality_json,
                "is_blocked_by_geo": bool(release_payload.get("is_blocked_by_geo")),
            }
        if "is_blocked_by_copyrights" in release_payload:
            quality_json = {
                **quality_json,
                "is_blocked_by_copyrights": bool(
                    release_payload.get("is_blocked_by_copyrights")
                ),
            }
        return quality_json

    @staticmethod
    def _to_file_size(torrent_payload: dict[str, Any]) -> int | None:
        value = torrent_payload.get("size")
        if isinstance(value, int):
            return value
        return None

    @staticmethod
    def _extract_torrent_description(torrent_payload: dict[str, Any]) -> str | None:
        return TorrentArchiveService._clean_text(torrent_payload.get("description"))

    @staticmethod
    def _parse_api_datetime(raw: Any) -> datetime | None:
        """AniLibria OpenAPI date-time → naive UTC; иначе None.

        Контракт TZ: API отдаёт UTC (`Z` / `+00:00`); UI сайта — wall-clock UTC+7.
        Z/+offset всегда конвертируем в UTC и снимаем tzinfo (как ``utcnow()`` и БД).
        Naive строку без offset считаем уже UTC (не локаль сервера / не UTC+7).
        Ошибка «срезать Z и оставить цифры» или «принять 23:07 UTC+7 за UTC»
        даёт бейдж overdue со сдвигом ±7ч.
        """
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw
            return raw.astimezone(timezone.utc).replace(tzinfo=None)
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _extract_api_created_at(torrent_payload: dict[str, Any]) -> datetime | None:
        """Часы загрузки версии на AL для SLA: max(created_at, updated_at).

        ``created_at`` — первая заливка torrent_id; ``updated_at`` — republish /
        расширение батча (дата в UI AniLibria). Без updated_at SLA даёт сотни
        часов от первой заливки (баг «371ч» при актуальном updated ~сутки назад).
        """
        candidates = [
            TorrentArchiveService._parse_api_datetime(torrent_payload.get("created_at")),
            TorrentArchiveService._parse_api_datetime(torrent_payload.get("updated_at")),
        ]
        present = [value for value in candidates if value is not None]
        if not present:
            return None
        return max(present)

    @staticmethod
    def _extract_api_info_hash(torrent_payload: dict[str, Any]) -> str | None:
        """info_hash из list/get payload; битый/пустой → None."""
        raw_hash = torrent_payload.get("info_hash") or torrent_payload.get("hash")
        if raw_hash is None or not str(raw_hash).strip():
            return None
        try:
            return sanitize_info_hash(str(raw_hash).strip())
        except ValueError:
            return None

    @staticmethod
    def _normalize_row_info_hash(row: Any) -> str | None:
        raw = getattr(row, "info_hash", None)
        if raw is None or not str(raw).strip():
            return None
        try:
            return sanitize_info_hash(str(raw).strip())
        except ValueError:
            return None

    @staticmethod
    def _extract_torrent_type(torrent_payload: dict[str, Any]) -> str | None:
        return TorrentArchiveService.extract_torrent_type(torrent_payload)

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None

    @staticmethod
    def extract_torrent_type(torrent_payload: dict[str, Any]) -> str | None:
        """Тип для UI/qB: например «WEBRip 1080p HEVC» (type + quality + codec)."""
        parts: list[str] = []

        rip_type = torrent_payload.get("type")
        if isinstance(rip_type, dict):
            for key in ("value", "description", "label"):
                text = TorrentArchiveService._clean_text(rip_type.get(key))
                if text:
                    parts.append(text)
                    break

        quality = torrent_payload.get("quality")
        if isinstance(quality, dict):
            for key in ("value", "description", "label"):
                text = TorrentArchiveService._clean_text(quality.get(key))
                if text:
                    parts.append(text)
                    break

        codec = torrent_payload.get("codec")
        if isinstance(codec, dict):
            text = TorrentArchiveService._clean_text(codec.get("label"))
            if not text:
                text = TorrentArchiveService._clean_text(codec.get("value"))
            if text:
                # value вида x265/HEVC → предпочитаем короткий label HEVC
                parts.append(text)

        if parts:
            return " ".join(parts)

        label = torrent_payload.get("label")
        if isinstance(label, str) and label.strip():
            # Fallback: фрагмент в квадратных скобках из label
            brackets = re.findall(r"\[([^\]]+)\]", label)
            if brackets:
                # обычно [BDRip 1080p][HEVC][1-189] → берём первые без серий
                useful = [b.strip() for b in brackets if b.strip() and not re.fullmatch(r"[\d\-\s]+", b)]
                if useful:
                    return " ".join(useful[:3])
            for token in ("AV1", "HEVC", "AVC", "x265", "x264"):
                if token in label.upper():
                    return token
        return None

    def _find_archive_for_upsert(self, torrent_id: int, info_hash: str) -> TorrentArchive | None:
        """Найти строку для обновления без воскрешения чужой superseded-версии.

        1) активная запись с тем же info_hash
        2) активная запись того же torrent_id (возможно другой hash → supersede)
        3) superseded с тем же hash, только если нет другой активной версии torrent_id
        """
        normalized = (info_hash or "").strip().lower()
        active_same_hash = self._db.scalar(
            select(TorrentArchive)
            .where(
                TorrentArchive.info_hash == normalized,
                TorrentArchive.superseded.is_(False),
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if active_same_hash is not None:
            return active_same_hash

        active_same_torrent = self._db.scalar(
            select(TorrentArchive)
            .where(
                TorrentArchive.torrent_id == torrent_id,
                TorrentArchive.superseded.is_(False),
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )
        if active_same_torrent is not None:
            return active_same_torrent

        # Нет активных: можно поднять историю с тем же hash (ре-добавление).
        return self._db.scalar(
            select(TorrentArchive)
            .where(
                TorrentArchive.info_hash == normalized,
                TorrentArchive.torrent_id == torrent_id,
                TorrentArchive.superseded.is_(True),
            )
            .order_by(TorrentArchive.id.desc())
            .limit(1)
        )

    def save_torrent(
        self,
        *,
        torrent_bytes: bytes,
        info_hash: str,
        release_id: int,
        release_alias: str | None,
        torrent_payload: dict[str, Any],
        release_payload: dict[str, Any],
    ) -> TorrentArchive:
        safe_hash = sanitize_info_hash(info_hash)
        self._storage_root.mkdir(parents=True, exist_ok=True)
        relative_path = Path("data") / "torrents" / f"{safe_hash}.torrent"
        absolute_path = self._storage_root / f"{safe_hash}.torrent"
        absolute_path.write_bytes(torrent_bytes)

        torrent_id = int(torrent_payload["id"])
        torrent_description = self._extract_torrent_description(torrent_payload)
        torrent_type = self._extract_torrent_type(torrent_payload)
        api_created_at = self._extract_api_created_at(torrent_payload)
        quality_json = self._attach_release_names(
            self._build_quality_json(torrent_payload),
            release_payload,
        )
        archive = self._find_archive_for_upsert(torrent_id, safe_hash)
        if archive is not None and (archive.info_hash or "").strip().lower() != safe_hash.lower():
            # Новая версия того же torrent_id — старую оставляем в истории релиза.
            archive.superseded = True
            archive.api_present = False
            self._db.commit()
            archive = None

        if archive is None:
            archive = TorrentArchive(
                info_hash=safe_hash,
                torrent_id=torrent_id,
                release_id=release_id,
                release_alias=release_alias,
                anime_name=self._extract_anime_name(release_payload),
                category=self._build_category(release_payload),
                description=self._clean_text(release_payload.get("description")),
                torrent_description=torrent_description,
                torrent_type=torrent_type,
                quality_json=quality_json,
                file_path=str(relative_path),
                file_size=self._to_file_size(torrent_payload),
                api_created_at=api_created_at,
                api_present=True,
                superseded=False,
            )
            self._db.add(archive)
        else:
            archive.info_hash = safe_hash
            archive.torrent_id = torrent_id
            archive.release_id = release_id
            # Не затираем уже сохранённый alias пустым значением — иначе slave/retry без URL.
            if release_alias:
                archive.release_alias = release_alias
            archive.anime_name = self._extract_anime_name(release_payload)
            archive.category = self._build_category(release_payload)
            archive.description = self._clean_text(release_payload.get("description"))
            archive.torrent_description = torrent_description
            archive.torrent_type = torrent_type
            archive.quality_json = quality_json
            archive.file_path = str(relative_path)
            archive.file_size = self._to_file_size(torrent_payload)
            # Тот же info_hash = тот же SLA clock. Не двигаем уже заполненный
            # api_created_at (правка description на AL бампает updated_at).
            # Пустой clock заполняем; новый max(created, updated) — только CREATE
            # новой версии (другой hash, строка выше).
            if archive.api_created_at is None and api_created_at is not None:
                archive.api_created_at = api_created_at
            archive.api_present = True
            archive.superseded = False
            # Обновление той же версии (тот же info_hash) — ignore_hevc сохраняем.
            # Новая версия (supersede выше) создаёт строку с ignore_hevc=False.

        # Карточка релиза (состав/блокировки/жанры) — source of truth рядом с dual-write в quality_json.
        if isinstance(release_payload, dict) and release_payload:
            from app.services.release_meta import upsert_release_meta

            upsert_release_meta(self._db, release_id, release_payload, commit=False)

        self._db.commit()
        self._db.refresh(archive)
        return archive

    def _find_active_archive(
        self,
        *,
        torrent_id: int | None = None,
        info_hash: str | None = None,
    ) -> TorrentArchive | None:
        """Активная (не superseded) запись по torrent_id и/или info_hash."""
        if info_hash:
            try:
                normalized = sanitize_info_hash(info_hash)
            except ValueError:
                normalized = None
            if normalized:
                row = self._db.scalar(
                    select(TorrentArchive)
                    .where(
                        TorrentArchive.info_hash == normalized,
                        TorrentArchive.superseded.is_(False),
                    )
                    .order_by(TorrentArchive.id.desc())
                    .limit(1)
                )
                if row is not None:
                    return row
        if torrent_id is not None:
            return self._db.scalar(
                select(TorrentArchive)
                .where(
                    TorrentArchive.torrent_id == torrent_id,
                    TorrentArchive.superseded.is_(False),
                )
                .order_by(TorrentArchive.id.desc())
                .limit(1)
            )
        return None

    def update_archive_meta_from_api_payload(
        self,
        *,
        release_id: int,
        torrent_payload: dict[str, Any],
        release_payload: dict[str, Any] | None = None,
        release_alias: str | None = None,
    ) -> str:
        """Meta-only обновление архива из list/get payload без скачивания .torrent.

        Returns:
            ``updated`` — поля записаны;
            ``noop`` — изменений нет;
            ``missing`` — нет активной строки архива;
            ``hash_mismatch`` — info_hash в API ≠ архив (нужен обычный sync/handoff).
        """
        raw_id = torrent_payload.get("id") or torrent_payload.get("torrent_id")
        try:
            torrent_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            torrent_id = None
        if torrent_id is None:
            return "missing"

        api_hash = self._extract_api_info_hash(torrent_payload)

        archive = self._find_active_archive(torrent_id=torrent_id, info_hash=api_hash)
        if archive is None:
            return "missing"

        archived_hash = (archive.info_hash or "").strip().lower()
        if api_hash and archived_hash and archived_hash != api_hash:
            return "hash_mismatch"

        changed = False
        torrent_description = self._extract_torrent_description(torrent_payload)
        if torrent_description is not None and archive.torrent_description != torrent_description:
            archive.torrent_description = torrent_description
            changed = True

        torrent_type = self._extract_torrent_type(torrent_payload)
        if torrent_type is not None and archive.torrent_type != torrent_type:
            archive.torrent_type = torrent_type
            changed = True

        tech = self._build_quality_json(torrent_payload)
        quality = dict(archive.quality_json) if isinstance(archive.quality_json, dict) else {}
        # Tech-поля из list; genres/members/names/blocks не затираем сырым list.
        for key, value in tech.items():
            if quality.get(key) != value:
                quality[key] = value
                changed = True

        if isinstance(release_payload, dict) and release_payload:
            anime_name = self._extract_anime_name(release_payload)
            if anime_name is not None and archive.anime_name != anime_name:
                archive.anime_name = anime_name
                changed = True
            category = self._build_category(release_payload)
            if category and archive.category != category:
                archive.category = category
                changed = True
            description = self._clean_text(release_payload.get("description"))
            if description is not None and archive.description != description:
                archive.description = description
                changed = True
            before_attach = dict(quality)
            quality = self._attach_release_names(quality, release_payload)
            if quality != before_attach:
                changed = True

        if release_alias:
            cleaned_alias = release_alias.strip().strip("/") or None
            if cleaned_alias and archive.release_alias != cleaned_alias:
                archive.release_alias = cleaned_alias
                changed = True

        if archive.release_id != release_id:
            archive.release_id = release_id
            changed = True

        # Та же версия (hash_mismatch уже вышли): не увеличиваем SLA clock.
        # updated_at после правки description на AL не даёт новые 24 часа.
        api_created_at = self._extract_api_created_at(torrent_payload)
        if api_created_at is not None and archive.api_created_at is None:
            archive.api_created_at = api_created_at
            changed = True

        if archive.quality_json != quality:
            archive.quality_json = quality
            changed = True

        if not archive.api_present:
            archive.api_present = True
            changed = True

        if changed:
            self._db.commit()
            return "updated"
        return "noop"

    def fill_missing_api_created_at(
        self,
        release_id: int,
        torrents: list[dict[str, Any]],
    ) -> int:
        """Заполнить пустой api_created_at из list payload (full_sync/ongoing).

        Тот же info_hash = тот же SLA clock: уже заполненный clock не двигаем
        (правка description на AL бампает updated_at, это не новая версия).
        Если hash в payload другой — строку не трогаем (скоро supersede;
        новый clock только у новой строки в save_torrent).
        Игнорирует битый/пустой payload. Не трогает superseded / api_present=False:
        AniLibria переиспользует torrent_id при 1-8→1-9, и новый updated_at
        относится только к активной версии.
        """
        by_tid: dict[int, tuple[datetime, str | None]] = {}
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            raw_id = torrent.get("id") or torrent.get("torrent_id")
            try:
                tid = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                tid = None
            if tid is None:
                continue
            parsed = self._extract_api_created_at(torrent)
            if parsed is not None:
                by_tid[tid] = (parsed, self._extract_api_info_hash(torrent))
        if not by_tid:
            return 0
        rows = list(
            self._db.scalars(
                select(TorrentArchive).where(
                    TorrentArchive.release_id == release_id,
                    TorrentArchive.torrent_id.in_(list(by_tid.keys())),
                    TorrentArchive.superseded.is_(False),
                    TorrentArchive.api_present.is_(True),
                )
            ).all()
        )
        updated = 0
        for row in rows:
            if bool(getattr(row, "superseded", False)) or not bool(
                getattr(row, "api_present", True)
            ):
                continue
            packed = by_tid.get(int(row.torrent_id))
            if packed is None:
                continue
            value, payload_hash = packed
            row_hash = self._normalize_row_info_hash(row)
            if payload_hash and row_hash and payload_hash != row_hash:
                # Новая версия: clock этой строки не наш, её supersede'нут.
                continue
            if row.api_created_at is not None:
                continue
            row.api_created_at = value
            updated += 1
        if updated:
            self._db.commit()
        return updated

    def list_archive(self, *, page: int, per_page: int, search: str | None) -> dict[str, Any]:
        filters = []
        if search:
            filters.append(TorrentArchive.anime_name.ilike(f"%{search}%"))

        total_query = select(func.count(TorrentArchive.id))
        items_query = select(TorrentArchive)
        if filters:
            total_query = total_query.where(*filters)
            items_query = items_query.where(*filters)

        total = self._db.scalar(total_query) or 0
        items = self._db.scalars(
            items_query.order_by(TorrentArchive.id.desc()).offset((page - 1) * per_page).limit(per_page)
        ).all()
        return {
            "items": [self.serialize_item(item) for item in items],
            "page": page,
            "per_page": per_page,
            "total": total,
        }

    def get_archive(self, archive_id: int) -> TorrentArchive | None:
        return self._db.get(TorrentArchive, archive_id)

    def resolve_file_path(self, archive: TorrentArchive) -> Path:
        stored = Path(archive.file_path)
        if stored.is_absolute():
            return stored
        return self._storage_root / stored.name

    @staticmethod
    def serialize_item(item: TorrentArchive) -> dict[str, Any]:
        return {
            "id": item.id,
            "anime_name": item.anime_name,
            "category": item.category,
            "description": item.description,
            "torrent_description": item.torrent_description,
            "torrent_type": item.torrent_type,
            "quality_json": item.quality_json,
            "release_alias": item.release_alias,
            "release_id": item.release_id,
            "torrent_id": item.torrent_id,
            "file_size": item.file_size,
            "info_hash": item.info_hash,
            "file_path": item.file_path,
            "created_at": as_utc_iso(item.created_at),
        }
