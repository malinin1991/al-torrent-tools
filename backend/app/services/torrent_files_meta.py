"""Парсинг списка файлов из .torrent и резолв путей через qB master."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import qbittorrentapi
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.qbittorrent import _decode_bencode

logger = logging.getLogger(__name__)

QB_INCOMPLETE_SUFFIX = ".!qB"


@dataclass(frozen=True)
class TorrentFileMeta:
    relative_path: str
    size: int
    file_index: int


def resolve_media_root() -> Path:
    return Path(settings.anilibria_media_root).resolve()


def is_under_media_root(path: Path, *, media_root: Path | None = None) -> bool:
    root = media_root or resolve_media_root()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def parse_torrent_file_list(torrent_bytes: bytes) -> list[TorrentFileMeta]:
    """Извлекает файлы из bencode info (multi-file или single-file)."""
    root, _ = _decode_bencode(torrent_bytes, 0)
    if not isinstance(root, dict):
        raise ValueError("Корень .torrent должен быть словарём")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("В .torrent отсутствует info")

    name_raw = info.get(b"name", b"")
    name = _bdecode_str(name_raw) if isinstance(name_raw, (bytes, str)) else "torrent"
    files_raw = info.get(b"files")
    result: list[TorrentFileMeta] = []

    if isinstance(files_raw, list):
        for index, item in enumerate(files_raw):
            if not isinstance(item, dict):
                continue
            length = item.get(b"length", 0)
            size = int(length) if isinstance(length, int) else 0
            path_parts = item.get(b"path")
            if not isinstance(path_parts, list) or not path_parts:
                continue
            parts = [_bdecode_str(p) for p in path_parts if isinstance(p, (bytes, str))]
            if not parts:
                continue
            # Всегда POSIX-разделители: иначе prior на Linux не матчится с путями из Windows/qB.
            relative = "/".join([name, *parts])
            result.append(TorrentFileMeta(relative_path=relative, size=size, file_index=index))
        return result

    length = info.get(b"length", 0)
    size = int(length) if isinstance(length, int) else 0
    result.append(TorrentFileMeta(relative_path=name.replace("\\", "/"), size=size, file_index=0))
    return result


def normalize_rel_path(path: str) -> str:
    """Единый вид relative_path для сравнения составов версий."""
    return (path or "").replace("\\", "/").strip("/")


def _bdecode_str(value: bytes | str) -> str:
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def extract_qb_save_path(torrent_info: Any) -> str | None:
    """Каталог загрузки (save_path), без content_path — иначе ломается join с name файла."""
    for attr in ("save_path", "savePath"):
        raw = getattr(torrent_info, attr, None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(torrent_info, dict):
            value = torrent_info.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_qb_content_path(torrent_info: Any) -> str | None:
    """Корень контента торрента (папка/файл) — запасной якорь для резолва."""
    for attr in ("content_path", "contentPath"):
        raw = getattr(torrent_info, attr, None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(torrent_info, dict):
            value = torrent_info.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_qb_file_priorities(qb_files: Any) -> dict[int, int]:
    """index → priority (0 = не выбран)."""
    priorities: dict[int, int] = {}
    if not qb_files:
        return priorities
    for item in qb_files:
        index = getattr(item, "index", None)
        if index is None and isinstance(item, dict):
            index = item.get("index")
        priority = getattr(item, "priority", None)
        if priority is None and isinstance(item, dict):
            priority = item.get("priority")
        if index is None:
            continue
        try:
            priorities[int(index)] = int(priority or 0)
        except (TypeError, ValueError):
            continue
    return priorities


def resolve_full_path(
    save_path: str,
    relative_path: str,
    *,
    content_path: str | None = None,
    media_root: Path | None = None,
) -> Path | None:
    """Абсолютный путь под ANILIBRIA_MEDIA_ROOT.

    Учитывает типичные раскладки qB:
    - save_path=/anilibria/2012 + name=Show/ep.mkv
    - save_path уже = .../Show, а name всё ещё Show/ep.mkv (без дубля)
    - content_path как запасной якорь
    """
    root = media_root or resolve_media_root()
    rel = Path(relative_path)
    bases: list[Path] = []
    if save_path.strip():
        bases.append(Path(save_path))
    if content_path and content_path.strip():
        bases.append(Path(content_path))

    candidates: list[Path] = []
    if rel.is_absolute():
        candidates.append(rel)
    else:
        for base in bases:
            # Если base уже = корневая папка торрента, а name начинается с неё —
            # сначала варианты без дубля (иначе fallback вернёт несуществующий путь).
            if rel.parts and base.name == rel.parts[0]:
                candidates.append(base.parent / rel)
                if len(rel.parts) > 1:
                    candidates.append(base / Path(*rel.parts[1:]))
            candidates.append(base / rel)
        # content_path = Show, name = Show/ep.mkv → Show/ep.mkv
        if content_path and rel.parts and Path(content_path).name == rel.parts[0] and len(rel.parts) > 1:
            candidates.append(Path(content_path) / Path(*rel.parts[1:]))

    seen: set[str] = set()
    under_root: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if not is_under_media_root(resolved, media_root=root):
            continue
        under_root.append(resolved)
        if resolved.is_file() or path_exists_including_incomplete(resolved):
            return resolved

    if under_root:
        # Файл ещё не на диске: предпочитаем кандидата с существующим parent dir.
        for candidate in under_root:
            try:
                if candidate.parent.is_dir():
                    return candidate
            except OSError:
                continue
        return under_root[0]

    logger.warning(
        "Путь вне ANILIBRIA_MEDIA_ROOT или не резолвится: save_path=%s content_path=%s relative=%s",
        save_path,
        content_path,
        relative_path,
    )
    return None


def extract_qb_file_name(qb_file: Any) -> str | None:
    """Относительный путь файла из torrents/files."""
    for attr in ("name", "file_name", "fileName"):
        raw = getattr(qb_file, attr, None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(qb_file, dict):
            value = qb_file.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_qb_torrent_hash(torrent: Any) -> str | None:
    for attr in ("hash", "infohash_v1", "info_hash"):
        raw = getattr(torrent, attr, None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
        if isinstance(torrent, dict):
            value = torrent.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return None


def collect_known_paths_from_qb(
    db: Session,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> set[Path]:
    """Пути файлов из master qB под ANILIBRIA_MEDIA_ROOT (без хеширования)."""
    from sqlalchemy import select

    from app.db.models import QbClient

    media_root = resolve_media_root()
    master = db.scalar(select(QbClient).where(QbClient.role == "master", QbClient.enabled.is_(True)).limit(1))
    if master is None:
        if log_fn:
            log_fn("qB master не настроен — пути из клиента недоступны")
        return set()

    qb = qbittorrentapi.Client(
        host=master.host,
        port=master.port,
        username=master.username,
        password=master.password_encrypted,
    )
    qb.auth_log_in()

    known: set[Path] = set()
    torrents = list(qb.torrents_info() or [])
    under_root = 0
    for index, torrent in enumerate(torrents, start=1):
        save_path = extract_qb_save_path(torrent)
        content_path = extract_qb_content_path(torrent)
        if not save_path and not content_path:
            continue
        anchor_raw = save_path or content_path or ""
        base = Path(anchor_raw).resolve()
        anchor = base if base.is_dir() else base.parent
        if not is_under_media_root(anchor, media_root=media_root):
            continue
        under_root += 1
        info_hash = extract_qb_torrent_hash(torrent)
        if not info_hash:
            continue
        if base.is_file() and is_under_media_root(base, media_root=media_root):
            known.add(base)

        try:
            qb_files = qb.torrents_files(torrent_hash=info_hash) or []
        except Exception as exc:
            if log_fn:
                log_fn(f"torrents_files {info_hash[:12]}…: {exc}")
            continue

        for qb_file in qb_files:
            rel_name = extract_qb_file_name(qb_file)
            if not rel_name:
                continue
            resolved = resolve_full_path(
                save_path or content_path or "",
                rel_name,
                content_path=content_path,
                media_root=media_root,
            )
            if resolved is not None:
                known.add(resolved.resolve())

        if log_fn and index % 100 == 0:
            log_fn(f"qB: обработано торрентов {index}/{len(torrents)}, путей={len(known)}")

    if log_fn:
        log_fn(f"qB: торрентов под {media_root}={under_root}, известных путей={len(known)}")
    return known


def path_exists_including_incomplete(path: Path) -> bool:
    """Файл есть или качается (суффикс .!qB)."""
    if path.is_file():
        return True
    incomplete = Path(str(path) + QB_INCOMPLETE_SUFFIX)
    return incomplete.is_file()


def is_incomplete_path(path: Path) -> bool:
    return path.name.endswith(QB_INCOMPLETE_SUFFIX) or Path(str(path) + QB_INCOMPLETE_SUFFIX).is_file()


def is_partial_only(path: Path | str) -> bool:
    """Качается: есть .!qB и нет complete-файла (не путать с соседним .!qB при уже готовом файле)."""
    canonical = complete_path_for(path)
    try:
        if canonical.is_file():
            return False
    except OSError:
        pass
    try:
        return Path(str(canonical) + QB_INCOMPLETE_SUFFIX).is_file()
    except OSError:
        return False


def complete_path_for(path: Path | str) -> Path:
    """Канонический путь без суффикса .!qB (как в torrent_files / disk_file_hashes)."""
    p = Path(path)
    name = p.name
    if name.endswith(QB_INCOMPLETE_SUFFIX):
        return p.with_name(name[: -len(QB_INCOMPLETE_SUFFIX)])
    return p
