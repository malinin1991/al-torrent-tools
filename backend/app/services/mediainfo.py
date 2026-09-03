"""Сервис извлечения и кэширования MediaInfo для медиафайлов."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FileMediaInfo, TorrentFile
from app.services.torrent_files_meta import (
    complete_path_for,
    is_incomplete_path,
    is_under_media_root,
    resolve_media_root,
)
from app.utils.datetime_fmt import utcnow

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = frozenset(
    {
        ".mkv",
        ".mp4",
        ".avi",
        ".m4v",
        ".mov",
        ".flv",
        ".wmv",
        ".webm",
        ".ts",
        ".m2ts",
        ".flac",
        ".mp3",
        ".aac",
        ".ac3",
        ".mka",
    }
)


def is_media_filename(path: Path | str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in MEDIA_EXTENSIONS


def format_duration_human(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    total = int(round(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours} ч")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} мин")
    parts.append(f"{secs} с")
    return " ".join(parts)


def format_bitrate_human(bps: int | float | None) -> str:
    if bps is None or bps <= 0:
        return ""
    num = float(bps)
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f} Мбит/с"
    if num >= 1_000:
        return f"{num / 1_000:.0f} кбит/с"
    return f"{num:.0f} бит/с"


def _build_summary_from_data(data: dict[str, Any]) -> dict[str, Any]:
    tracks = data.get("tracks") or []
    general: dict[str, Any] = {}
    video_list: list[dict[str, Any]] = []
    audio_list: list[dict[str, Any]] = []
    sub_list: list[dict[str, Any]] = []

    for tr in tracks:
        ttype = (tr.get("track_type") or "").strip().lower()
        if ttype == "general" and not general:
            general = tr
        elif ttype == "video":
            video_list.append(tr)
        elif ttype == "audio":
            audio_list.append(tr)
        elif ttype in {"text", "subtitles"}:
            sub_list.append(tr)

    # General / container info
    duration_raw = general.get("duration")
    duration_sec: float | None = None
    if duration_raw is not None:
        try:
            val = float(duration_raw)
            duration_sec = val / 1000.0 if val > 0 else 0.0
        except (ValueError, TypeError):
            pass

    overall_br = general.get("overall_bit_rate") or general.get("bit_rate")
    file_size_raw = general.get("file_size")

    # Video summary
    videos: list[dict[str, Any]] = []
    for v in video_list:
        fps_val = v.get("frame_rate")
        w_val = v.get("width")
        h_val = v.get("height")
        br_val = v.get("bit_rate")
        fps_str = ""
        if fps_val is not None and str(fps_val).strip():
            try:
                fps_str = f"{float(fps_val):.3f}".rstrip("0").rstrip(".")
            except (ValueError, TypeError):
                fps_str = str(fps_val)
        videos.append(
            {
                "stream_id": v.get("stream_identifier") or v.get("id"),
                "format": v.get("format") or "",
                "format_profile": v.get("format_profile") or "",
                "codec_id": v.get("codec_id") or "",
                "width": int(w_val) if w_val and str(w_val).isdigit() else None,
                "height": int(h_val) if h_val and str(h_val).isdigit() else None,
                "resolution": f"{w_val}x{h_val}" if w_val and h_val else "",
                "aspect_ratio": v.get("display_aspect_ratio") or "",
                "frame_rate": fps_str,
                "bit_rate": format_bitrate_human(br_val),
                "bit_depth": v.get("bit_depth"),
                "color_space": v.get("color_space") or "",
                "hdr_format": v.get("hdr_format") or v.get("hdr_format_commercial") or "",
            }
        )

    # Audio summary
    audios: list[dict[str, Any]] = []
    for a in audio_list:
        ch_val = a.get("channel_s") or a.get("channels")
        ch_layout = a.get("channel_layout") or ""
        br_val = a.get("bit_rate")
        sr_val = a.get("sampling_rate")
        audios.append(
            {
                "stream_id": a.get("stream_identifier") or a.get("id"),
                "language": (a.get("language") or "und").lower(),
                "title": a.get("title") or "",
                "format": a.get("format") or "",
                "format_profile": a.get("format_profile") or "",
                "channels": f"{ch_val} каналов" if ch_val else (ch_layout or ""),
                "bit_rate": format_bitrate_human(br_val),
                "sampling_rate": f"{int(sr_val) // 1000} кГц" if sr_val and str(sr_val).isdigit() else "",
            }
        )

    # Subtitles summary
    subs: list[dict[str, Any]] = []
    for s in sub_list:
        subs.append(
            {
                "stream_id": s.get("stream_identifier") or s.get("id"),
                "language": (s.get("language") or "und").lower(),
                "title": s.get("title") or "",
                "format": s.get("format") or "",
            }
        )

    return {
        "format": general.get("format") or "",
        "format_profile": general.get("format_profile") or "",
        "duration_sec": duration_sec,
        "duration_human": format_duration_human(duration_sec),
        "overall_bit_rate": format_bitrate_human(overall_br),
        "file_size": file_size_raw,
        "videos": videos,
        "audios": audios,
        "subtitles": subs,
    }


def _format_raw_text_report(data: dict[str, Any], path: Path) -> str:
    lines: list[str] = []
    tracks = data.get("tracks") or []
    lines.append(f"Файл: {path.name}")
    lines.append(f"Путь: {path}")
    lines.append("-" * 60)

    for tr in tracks:
        ttype = tr.get("track_type") or "Track"
        lines.append(f"\n[{ttype.upper()}]")
        for k, v in tr.items():
            if k in {"track_type", "other_format", "other_codec_id"}:
                continue
            if v is not None and str(v).strip():
                lines.append(f"  {k:28}: {v}")

    return "\n".join(lines).strip()


def parse_media_file(path: Path) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Извлечение MediaInfo через pymediainfo.

    Возвращает (summary_json, raw_json, raw_text) или None при ошибке.
    """
    if not path.is_file():
        return None

    try:
        from pymediainfo import MediaInfo
    except ImportError:
        logger.error("pymediainfo не установлена")
        return None

    try:
        # Проверяем возможность парсинга
        if hasattr(MediaInfo, "can_parse") and not MediaInfo.can_parse():
            logger.warning("Библиотека libmediainfo не найдена в системе")

        mi = MediaInfo.parse(str(path))
        raw_dict = mi.to_data()
        summary = _build_summary_from_data(raw_dict)
        raw_text = _format_raw_text_report(raw_dict, path)
        return summary, raw_dict, raw_text
    except Exception as exc:
        logger.warning("Ошибка парсинга MediaInfo для %s: %s", path, exc)
        return None


def get_canonical_path(path: Path | str) -> str:
    p = Path(path)
    try:
        return str(complete_path_for(p).resolve())
    except OSError:
        return str(complete_path_for(p))


def upsert_file_mediainfo(
    db: Session,
    full_path: str,
    *,
    force: bool = False,
) -> FileMediaInfo | None:
    """Извлекает и сохраняет mediainfo в file_mediainfo с gate по size+mtime."""
    if not full_path:
        return None

    path = Path(full_path)
    if is_incomplete_path(path) or not path.is_file():
        return None

    canonical = get_canonical_path(path)
    try:
        stat = path.stat()
        file_size = stat.st_size
        mtime = float(stat.st_mtime)
    except OSError:
        return None

    existing = db.scalar(
        select(FileMediaInfo).where(FileMediaInfo.full_path == canonical).limit(1)
    )

    if existing is not None and not force:
        # Gate: если размер и mtime совпадают, повторно не парсим
        if existing.file_size == file_size and abs(existing.mtime - mtime) < 0.001:
            return existing

    parsed = parse_media_file(path)
    if parsed is None:
        return None

    summary, raw_json, raw_text = parsed
    now = utcnow()

    if existing is None:
        existing = FileMediaInfo(
            full_path=canonical,
            file_size=file_size,
            mtime=mtime,
            summary_json=summary,
            raw_json=raw_json,
            raw_text=raw_text,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
    else:
        existing.file_size = file_size
        existing.mtime = mtime
        existing.summary_json = summary
        existing.raw_json = raw_json
        existing.raw_text = raw_text
        existing.updated_at = now

    db.commit()
    db.refresh(existing)
    return existing


def get_or_extract_mediainfo(
    db: Session,
    file_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Получить MediaInfo для файла.

    Если в базе пусто, но файл существует на диске — выполняет on-demand
    парсинг и сохраняет результат в БД (требование 3).
    """
    row = db.get(TorrentFile, file_id)
    if row is None:
        return {
            "ok": False,
            "file_id": file_id,
            "status": "not_found",
            "error": "Файл не найден в базе данных",
        }

    full_path_str = row.full_path
    if not full_path_str:
        return {
            "ok": False,
            "file_id": file_id,
            "status": "missing_path",
            "error": "Путь к файлу ещё не определен",
        }

    path = Path(full_path_str)
    canonical = get_canonical_path(path)

    # Проверка на продолжающуюся загрузку
    if is_incomplete_path(path) or bool(getattr(row, "is_checking", False)):
        return {
            "ok": False,
            "file_id": file_id,
            "status": "in_progress",
            "error": "Файл проверяется или ещё загружается на master",
        }

    # Поиск в кэше БД
    existing = db.scalar(
        select(FileMediaInfo).where(FileMediaInfo.full_path == canonical).limit(1)
    )

    if existing is not None and not force:
        # Проверяем, существует ли файл и не изменился ли он
        if path.is_file():
            try:
                st = path.stat()
                if existing.file_size == st.st_size and abs(existing.mtime - st.st_mtime) < 0.001:
                    return {
                        "ok": True,
                        "file_id": file_id,
                        "status": "ready",
                        "full_path": canonical,
                        "relative_path": row.relative_path,
                        "summary": existing.summary_json,
                        "raw_text": existing.raw_text,
                    }
            except OSError:
                pass
        else:
            # Файла нет на диске, но есть в кэше
            return {
                "ok": True,
                "file_id": file_id,
                "status": "ready",
                "full_path": canonical,
                "relative_path": row.relative_path,
                "summary": existing.summary_json,
                "raw_text": existing.raw_text,
                "cached_only": True,
            }

    # В базе пусто или force: парсим на лету с диска
    if not path.is_file():
        # Возможно файл без суффикса не найден, проверим complete_path_for
        canon_path = complete_path_for(path)
        if canon_path.is_file():
            path = canon_path
        else:
            return {
                "ok": False,
                "file_id": file_id,
                "status": "file_missing",
                "error": "Файл отсутствует на диске или ещё не скачан",
            }

    saved = upsert_file_mediainfo(db, str(path), force=force)
    if saved is None:
        return {
            "ok": False,
            "file_id": file_id,
            "status": "error",
            "error": "Не удалось извлечь MediaInfo для файла (возможно, файл поврежден или это не медиафайл)",
        }

    return {
        "ok": True,
        "file_id": file_id,
        "status": "ready",
        "full_path": saved.full_path,
        "relative_path": row.relative_path,
        "summary": saved.summary_json,
        "raw_text": saved.raw_text,
    }
