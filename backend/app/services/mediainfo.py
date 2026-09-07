"""Сервис извлечения и кэширования MediaInfo для медиафайлов."""

from __future__ import annotations

import logging
import os
import subprocess
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


def resolve_mediainfo_version() -> str:
    """Версия libmediainfo / CLI, которую использует приложение через pymediainfo.

    Сначала библиотека (тот же путь, что у parse), иначе ``mediainfo --Version``.
    При отсутствии — «не установлен» или текст ошибки.
    """
    lib_ver = _mediainfo_library_version()
    if lib_ver:
        return lib_ver
    return _mediainfo_cli_version()


def _mediainfo_library_version() -> str | None:
    try:
        from pymediainfo import MediaInfo
    except ImportError:
        return None
    try:
        if hasattr(MediaInfo, "can_parse") and not MediaInfo.can_parse():
            return None
        get_lib = getattr(MediaInfo, "_get_library", None)
        if not callable(get_lib):
            return None
        result = get_lib()
        # (CDLL/WinDLL, handle, version_str, version_tuple)
        if isinstance(result, tuple) and len(result) >= 3:
            version_str = result[2]
            if version_str:
                return f"libmediainfo {version_str}"
    except Exception as exc:
        logger.debug("Не удалось получить версию libmediainfo: %s", exc)
    return None


def _mediainfo_cli_version() -> str:
    try:
        completed = subprocess.run(
            ["mediainfo", "--Version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return "не установлен"
    except Exception as exc:
        return str(exc)

    out = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    if not out:
        return "не установлен"
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        if "MediaInfoLib" in ln or "libmediainfo" in ln.lower():
            return ln
    return lines[0] if lines else "не установлен"

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


def format_file_size_human(size_bytes: int | float | str | None) -> str:
    if size_bytes is None:
        return ""
    try:
        num = float(size_bytes)
    except (ValueError, TypeError):
        return ""
    if num <= 0:
        return ""
    if num >= 1_073_741_824:
        return f"{num / 1_073_741_824:.2f} ГиБ"
    if num >= 1_048_576:
        return f"{num / 1_048_576:.1f} МиБ"
    if num >= 1_024:
        return f"{num / 1_024:.0f} КиБ"
    return f"{num:.0f} Б"


def _parse_size_bytes(value: Any) -> int | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (ValueError, TypeError):
        return None
    if num <= 0:
        return None
    return int(num)


def _ternary_flag(value: Any) -> bool | None:
    """Нормализация MediaInfo Yes/No → true/false/null."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"yes", "true", "1", "y"}:
        return True
    if text in {"no", "false", "0", "n"}:
        return False
    return None


def _service_kind_tokens(track: dict[str, Any]) -> list[str]:
    """Собирает токены из ServiceKind и ServiceKind/String (оба поля MediaInfo)."""
    tokens: list[str] = []
    for key in ("service_kind", "service_kind_string"):
        raw = track.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, list):
            parts = [str(x) for x in raw]
        else:
            parts = str(raw).replace(",", "/").split("/")
        for part in parts:
            cleaned = part.strip().lower()
            if cleaned:
                tokens.append(cleaned)
    return tokens


def _track_original(
    track: dict[str, Any],
    *,
    default: bool | None,
    forced: bool | None,
) -> bool | None:
    """Matroska FlagOriginal / ServiceKind, без путаницы с title==«Original».

    MediaInfo для FlagOriginal пишет ServiceKind=``O`` и/или
    ServiceKind/String=``Original``. Явные Yes/No-поля (``original``,
    ``flag_original``, …) тоже учитываем; свободный текст вроде
    Original/Track name через ``_ternary_flag`` даёт None и игнорируется.
    """
    for key in ("original", "flag_original", "original_flag", "original_track"):
        if key not in track:
            continue
        flag = _ternary_flag(track.get(key))
        if flag is not None:
            return flag

    tokens = _service_kind_tokens(track)
    # Matroska: ServiceKind "O" + String "Original" (не путать с title).
    if any(t == "o" or t == "original" or "original" in t for t in tokens):
        return True

    if default is not None or forced is not None:
        # Контейнер отдал track flags — Original явно нет → false
        return False
    return None


def _track_flags(track: dict[str, Any]) -> dict[str, bool | None]:
    default = _ternary_flag(track.get("default") if "default" in track else track.get("default_track"))
    if "forced" in track:
        forced = _ternary_flag(track.get("forced"))
    else:
        forced = _ternary_flag(track.get("forced_track") or track.get("forced_display"))
    original = _track_original(track, default=default, forced=forced)
    return {"default": default, "forced": forced, "original": original}


_ENCODING_LIKE_FORMATS = frozenset(
    {
        "utf-8",
        "utf8",
        "utf-16",
        "utf16",
        "utf-16le",
        "utf-16be",
        "utf-32",
        "ascii",
    }
)

_CODEC_ID_FORMAT_MAP = {
    "s_text/utf8": "Plain Text",
    "s_text/ass": "ASS",
    "s_text/ssa": "SSA",
    "s_hdmv/pgs": "PGS",
    "s_vobsub": "VobSub",
    "s_hdmv/textst": "TextST",
    "s_kates": "Kate",
}


def _looks_like_encoding(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lower = text.lower().replace("_", "-")
    if lower in _ENCODING_LIKE_FORMATS:
        return True
    if lower.startswith("iso-8859-"):
        return True
    if lower.startswith("windows-125") or lower.startswith("cp125"):
        return True
    if lower.startswith("utf-"):
        return True
    return False


def _subtitle_format_from_codec(track: dict[str, Any], encoding: str) -> str:
    codec_id = str(track.get("codec_id") or "").strip()
    codec_key = codec_id.lower()
    if codec_key in _CODEC_ID_FORMAT_MAP:
        return _CODEC_ID_FORMAT_MAP[codec_key]

    info = str(track.get("codec_id_info") or "").strip()
    if info:
        cleaned = info
        if encoding:
            # "UTF-8 Plain Text" → "Plain Text"
            prefix = encoding.strip()
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix) :].strip(" -_")
        if cleaned and not _looks_like_encoding(cleaned):
            return cleaned
    return codec_id or ""


def _subtitle_format_and_encoding(track: dict[str, Any]) -> tuple[str, str]:
    raw_format = str(track.get("format") or "").strip()
    explicit_encoding = str(
        track.get("encoding") or track.get("character_set") or ""
    ).strip()

    if raw_format and _looks_like_encoding(raw_format):
        encoding = raw_format
        fmt = _subtitle_format_from_codec(track, encoding)
        return fmt or "Plain Text", encoding

    return raw_format, explicit_encoding


def _is_cover_attachment(track: dict[str, Any]) -> bool:
    type_val = str(track.get("type") or track.get("attachment_type") or "").strip().lower()
    title = str(track.get("title") or "").strip().lower()
    cover_markers = ("cover", "thumbnail", "poster", "artwork", "album art")
    return any(m in type_val for m in cover_markers) or any(m in title for m in cover_markers)


def _is_font_attachment(track: dict[str, Any]) -> bool:
    ttype = (track.get("track_type") or "").strip().lower()
    if ttype not in {"image", "other", "attachment"}:
        return False
    if _is_cover_attachment(track):
        return False

    type_val = str(track.get("type") or track.get("muxing_mode") or "").strip().lower()
    mime = str(track.get("internet_media_type") or track.get("mime") or "").strip().lower()
    fmt = str(track.get("format") or "").strip().lower()
    name = " ".join(
        str(track.get(k) or "")
        for k in ("title", "complete_name", "file_name", "format")
    ).lower()

    if "attachment" in type_val:
        return True

    font_mimes = (
        "font/",
        "truetype",
        "opentype",
        "application/font",
        "application/x-font",
        "application/vnd.ms-opentype",
    )
    if any(m in mime for m in font_mimes):
        return True

    font_exts = (".ttf", ".otf", ".ttc", ".woff", ".woff2")
    if any(ext in name for ext in font_exts):
        return True
    if fmt in {"ttf", "otf", "ttc", "woff", "woff2", "truetype", "opentype"}:
        return True
    if "font" in type_val or "font" in mime:
        return True
    return False


def _collect_fonts(tracks: list[dict[str, Any]], general: dict[str, Any]) -> tuple[list[dict[str, Any]], str, int | None]:
    fonts: list[dict[str, Any]] = []
    total_bytes = 0
    has_any_size = False

    for tr in tracks:
        if not _is_font_attachment(tr):
            continue
        name = (
            str(tr.get("title") or "").strip()
            or str(tr.get("complete_name") or "").strip()
            or str(tr.get("file_name") or "").strip()
            or str(tr.get("format") or "").strip()
            or "font"
        )
        mime = str(tr.get("internet_media_type") or tr.get("mime") or "").strip()
        size_bytes = _parse_size_bytes(tr.get("stream_size") or tr.get("file_size"))
        if size_bytes is not None:
            has_any_size = True
            total_bytes += size_bytes
        fonts.append(
            {
                "name": name,
                "size": format_file_size_human(size_bytes),
                "size_bytes": size_bytes,
                "mime": mime,
            }
        )

    if not fonts:
        attachments_raw = general.get("attachments")
        if attachments_raw:
            names = [p.strip() for p in str(attachments_raw).split("/") if p.strip()]
            for name in names:
                lower = name.lower()
                if any(x in lower for x in ("cover", "thumbnail", "poster", "artwork")):
                    continue
                fonts.append({"name": name, "size": "", "size_bytes": None, "mime": ""})

    fonts_total_bytes: int | None = total_bytes if has_any_size else None
    fonts_total_size = format_file_size_human(fonts_total_bytes) if fonts_total_bytes is not None else ""
    return fonts, fonts_total_size, fonts_total_bytes


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
        br_val = v.get("bit_rate") or v.get("nominal_bit_rate")
        fps_str = ""
        if fps_val is not None and str(fps_val).strip():
            try:
                fps_str = f"{float(fps_val):.3f}".rstrip("0").rstrip(".")
            except (ValueError, TypeError):
                fps_str = str(fps_val)
        flags = _track_flags(v)
        videos.append(
            {
                "stream_id": v.get("stream_identifier") or v.get("id"),
                "title": v.get("title") or "",
                "language": (v.get("language") or "").lower(),
                "format": v.get("format") or "",
                "format_profile": v.get("format_profile") or "",
                "codec_id": v.get("codec_id") or "",
                "width": int(w_val) if w_val and str(w_val).isdigit() else None,
                "height": int(h_val) if h_val and str(h_val).isdigit() else None,
                "resolution": f"{w_val}x{h_val}" if w_val and h_val else "",
                "aspect_ratio": v.get("display_aspect_ratio") or "",
                "frame_rate": fps_str,
                "bit_rate": format_bitrate_human(br_val),
                "stream_size": format_file_size_human(v.get("stream_size")),
                "bit_depth": v.get("bit_depth"),
                "color_space": v.get("color_space") or "",
                "hdr_format": v.get("hdr_format") or v.get("hdr_format_commercial") or "",
                "default": flags["default"],
                "forced": flags["forced"],
                "original": flags["original"],
            }
        )

    # Audio summary
    audios: list[dict[str, Any]] = []
    for a in audio_list:
        ch_val = a.get("channel_s") or a.get("channels")
        ch_layout = a.get("channel_layout") or ""
        br_val = a.get("bit_rate") or a.get("nominal_bit_rate")
        sr_val = a.get("sampling_rate")
        sampling_rate = ""
        if sr_val is not None and str(sr_val).strip():
            try:
                hz = float(sr_val)
                if hz >= 1000:
                    sampling_rate = f"{hz / 1000:.1f}".rstrip("0").rstrip(".") + " кГц"
                elif hz > 0:
                    sampling_rate = f"{int(hz)} Гц"
            except (ValueError, TypeError):
                sampling_rate = str(sr_val)
        flags = _track_flags(a)
        audios.append(
            {
                "stream_id": a.get("stream_identifier") or a.get("id"),
                "language": (a.get("language") or "und").lower(),
                "title": a.get("title") or "",
                "format": a.get("format") or "",
                "format_profile": a.get("format_profile") or "",
                "channels": f"{ch_val} каналов" if ch_val else (ch_layout or ""),
                "bit_rate": format_bitrate_human(br_val),
                "stream_size": format_file_size_human(a.get("stream_size")),
                "sampling_rate": sampling_rate,
                "default": flags["default"],
                "forced": flags["forced"],
                "original": flags["original"],
            }
        )

    # Subtitles summary
    subs: list[dict[str, Any]] = []
    for s in sub_list:
        fmt, encoding = _subtitle_format_and_encoding(s)
        flags = _track_flags(s)
        subs.append(
            {
                "stream_id": s.get("stream_identifier") or s.get("id"),
                "language": (s.get("language") or "und").lower(),
                "title": s.get("title") or "",
                "format": fmt,
                "encoding": encoding,
                "stream_size": format_file_size_human(s.get("stream_size")),
                "default": flags["default"],
                "forced": flags["forced"],
                "original": flags["original"],
            }
        )

    fonts, fonts_total_size, fonts_total_bytes = _collect_fonts(tracks, general)

    return {
        "format": general.get("format") or "",
        "format_profile": general.get("format_profile") or "",
        "duration_sec": duration_sec,
        "duration_human": format_duration_human(duration_sec),
        "overall_bit_rate": format_bitrate_human(overall_br),
        "file_size": format_file_size_human(file_size_raw),
        "videos": videos,
        "audios": audios,
        "subtitles": subs,
        "fonts": fonts,
        "fonts_total_size": fonts_total_size,
        "fonts_total_bytes": fonts_total_bytes,
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


def _effective_summary(existing: FileMediaInfo) -> dict[str, Any]:
    """Summary для UI: при наличии raw_json всегда пересобираем (новые ключи).

    Старые кэши могли не содержать fonts/default/encoding — без файла на диске
    и без force-refresh отдаём актуальный summary из сохранённого raw_json.
    Если raw нет — graceful degradation на summary_json как есть.
    """
    raw = getattr(existing, "raw_json", None)
    if isinstance(raw, dict) and raw:
        try:
            return _build_summary_from_data(raw)
        except Exception:
            logger.warning(
                "Не удалось пересобрать MediaInfo summary из raw_json для %s",
                getattr(existing, "full_path", "?"),
                exc_info=True,
            )
    summary = getattr(existing, "summary_json", None)
    return summary if isinstance(summary, dict) else {}


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
        summary = _effective_summary(existing)
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
                        "summary": summary,
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
                "summary": summary,
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
