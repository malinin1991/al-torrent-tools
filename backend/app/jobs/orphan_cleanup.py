"""Поиск orphan-файлов, мусора и пустых папок под /anilibria (dry-run по умолчанию)."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog, TorrentFile
from app.services.file_hasher import format_file_size
from app.services.job_runner import JobStopRequested, is_stop_requested
from app.services.qb_inventory import InventoryResult, build_inventory, connect_master
from app.services.torrent_files_meta import (
    QB_INCOMPLETE_SUFFIX,
    is_junk_dir,
    is_junk_file,
    is_under_media_root,
    resolve_media_root,
)

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi", ".m2ts", ".ts"}


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def media_root_writable_status(media_root: Path) -> tuple[bool, str]:
    """Проверка записи в media_root. Возвращает (ok, detail)."""
    import os

    try:
        resolved = media_root.resolve()
    except OSError as exc:
        return False, f"resolve failed: {exc}"

    if not resolved.is_dir():
        return False, f"не каталог: {resolved}"

    mode = "—"
    try:
        st = resolved.stat()
        mode = oct(st.st_mode & 0o777)
    except OSError as exc:
        return False, f"stat failed: {exc}"

    access_w = os.access(resolved, os.W_OK)
    probe = resolved / ".altt_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        errno_name = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno else "?"
        hint = ""
        if exc.errno == errno.EROFS:
            hint = " — том смонтирован read-only (:ro); в compose нужен :rw и recreate контейнера"
        elif exc.errno == errno.EACCES:
            hint = " — нет прав (PUID/PGID / владелец share на Unraid)"
        return (
            False,
            f"write probe failed: {exc.__class__.__name__} errno={errno_name} "
            f"mode={mode} access_W_OK={access_w} path={resolved}{hint}",
        )

    try:
        probe.unlink(missing_ok=True)
    except OSError:
        # Файл создали — запись есть; unlink не критичен.
        pass
    return True, f"writable mode={mode} access_W_OK={access_w} path={resolved}"


def media_root_is_writable(media_root: Path) -> bool:
    """Проверка, что apply сможет удалять файлы (не read-only mount)."""
    ok, _ = media_root_writable_status(media_root)
    return ok


def find_orphan_files(
    *,
    media_root: Path,
    known: set[Path],
) -> list[Path]:
    if not media_root.is_dir():
        return []
    orphans: list[Path] = []
    for path in media_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(QB_INCOMPLETE_SUFFIX):
            continue
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        resolved = path.resolve()
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        if resolved in known:
            continue
        orphans.append(resolved)
    return sorted(orphans)


def find_junk_files(*, media_root: Path) -> list[Path]:
    """Мусорные файлы (.DS_Store, Thumbs.db, ._*, …) под media_root."""
    if not media_root.is_dir():
        return []
    junk: list[Path] = []
    for path in media_root.rglob("*"):
        if not path.is_file():
            continue
        if not is_junk_file(path):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        junk.append(resolved)
    return sorted(junk)


def find_junk_dirs(*, media_root: Path) -> list[Path]:
    """Служебные каталоги (__MACOSX, @eaDir, …), deepest-first."""
    if not media_root.is_dir():
        return []
    dirs: list[Path] = []
    root = media_root.resolve()
    for path in media_root.rglob("*"):
        if not path.is_dir():
            continue
        if not is_junk_dir(path):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == root:
            continue
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        dirs.append(resolved)
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    return dirs


def find_empty_dirs(*, media_root: Path) -> list[Path]:
    """Пустые каталоги под media_root (без самого корня), deepest-first."""
    if not media_root.is_dir():
        return []
    root = media_root.resolve()
    candidates: list[Path] = []
    for path in media_root.rglob("*"):
        if not path.is_dir():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == root:
            continue
        if not is_under_media_root(resolved, media_root=media_root):
            continue
        candidates.append(resolved)

    candidates.sort(key=lambda p: len(p.parts), reverse=True)
    empty: list[Path] = []
    for path in candidates:
        try:
            next(path.iterdir())
        except StopIteration:
            empty.append(path)
        except OSError:
            continue
    return empty


def _path_size_bytes(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                if child.is_file():
                    try:
                        total += int(child.stat().st_size)
                    except OSError:
                        continue
            return total
    except OSError:
        return 0
    return 0


def paths_total_size(paths: list[Path]) -> int:
    return sum(_path_size_bytes(path) for path in paths)


def _delete_files(
    paths: list[Path],
    *,
    log_fn,
    check_stop=None,
) -> tuple[int, int]:
    deleted = 0
    errors = 0
    for path in paths:
        if check_stop is not None:
            check_stop()
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            errors += 1
            log_fn(f"не удалось удалить файл {path}: {exc}", "error")
    return deleted, errors


def _delete_dirs_recursive(
    paths: list[Path],
    *,
    log_fn,
    check_stop=None,
) -> tuple[int, int]:
    """Удалить деревья junk-каталогов (shutil-подобно через rmtree вручную)."""
    import shutil

    deleted = 0
    errors = 0
    for path in paths:
        if check_stop is not None:
            check_stop()
        if not path.exists():
            continue
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as exc:
            errors += 1
            log_fn(f"не удалось удалить каталог {path}: {exc}", "error")
    return deleted, errors


def _remove_empty_dirs(
    media_root: Path,
    *,
    log_fn,
    check_stop=None,
) -> tuple[int, int]:
    """Несколько проходов deepest-first, пока появляются новые пустые."""
    deleted = 0
    errors = 0
    while True:
        if check_stop is not None:
            check_stop()
        empties = find_empty_dirs(media_root=media_root)
        if not empties:
            break
        progress = 0
        for path in empties:
            if check_stop is not None:
                check_stop()
            if not path.exists():
                continue
            try:
                path.rmdir()
                deleted += 1
                progress += 1
            except OSError as exc:
                errors += 1
                log_fn(f"не удалось удалить пустую папку {path}: {exc}", "error")
        if progress == 0:
            break
    return deleted, errors


def _log_preview(db: Session, job_id: int, label: str, paths: list[Path], *, limit: int = 100) -> None:
    preview = paths[:limit]
    for path in preview:
        _add_log(db, job_id, f"{label}: {path}")
    if len(paths) > len(preview):
        _add_log(db, job_id, f"orphan_cleanup: … ещё {len(paths) - len(preview)} ({label})")


def known_paths_for_orphan_scan(db: Session, inventory: InventoryResult) -> tuple[set[Path], int]:
    """Пути валидного inventory + пути failed_hashes из torrent_files (как prune).

    При частичном сбое torrents_files раздача в failed_hashes; без защиты её файлы
    ошибочно считались бы orphan и удалились бы в apply.
    """
    known: set[Path] = set()
    for item in inventory.files:
        if not item.full_path:
            continue
        try:
            known.add(Path(item.full_path).resolve())
        except OSError:
            continue

    protect_hashes = {(h or "").strip().lower() for h in inventory.failed_hashes if h}
    protected = 0
    if protect_hashes:
        rows = db.scalars(
            select(TorrentFile).where(func.lower(TorrentFile.info_hash).in_(protect_hashes))
        ).all()
        for row in rows:
            if not row.full_path:
                continue
            try:
                known.add(Path(row.full_path).resolve())
                protected += 1
            except OSError:
                continue
    return known, protected


async def run_orphan_cleanup(db: Session, job_id: int, params: dict[str, Any]) -> None:
    requested_dry_run = bool(params.get("dry_run", True))
    requested_apply = bool(params.get("apply", False)) and not requested_dry_run
    if settings.cleanup_allow_delete:
        dry_run = requested_dry_run
        apply = requested_apply
    else:
        dry_run = True
        apply = False
    media_root = resolve_media_root()
    _add_log(
        db,
        job_id,
        f"orphan_cleanup: media_root={media_root}, dry_run={dry_run}, apply={apply}",
    )
    if not settings.cleanup_allow_delete and requested_apply:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: запрошено удаление, но CLEANUP_ALLOW_DELETE=false — только отчёт",
            "warning",
        )
    if not media_root.is_dir():
        _add_log(db, job_id, f"orphan_cleanup: корень недоступен: {media_root}", "warning")
        return

    qb = connect_master(db)
    if qb is None:
        _add_log(db, job_id, "orphan_cleanup: master qB не настроен — abort", "error")
        return

    def log_fn(message: str) -> None:
        _add_log(db, job_id, f"orphan_cleanup: {message}", "debug")

    def apply_log(message: str, level: str = "info") -> None:
        _add_log(db, job_id, f"orphan_cleanup: {message}", level)

    # Те же валидные раздачи, что и hash_backfill (без «не зарегистрирован»).
    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "orphan_cleanup: остановка по запросу", "warning")
        raise JobStopRequested()
    inventory = build_inventory(db, qb, log_fn=log_fn)
    known, protected = known_paths_for_orphan_scan(db, inventory)
    failed_n = len(inventory.failed_hashes)
    _add_log(
        db,
        job_id,
        f"orphan_cleanup: валидных раздач={len(inventory.valid_hashes)}, "
        f"известных путей={len(known)}, failed_hashes={failed_n}, "
        f"protected_paths={protected}, invalid_skipped={inventory.skipped_invalid}",
    )

    scan_orphans = True
    # Hard-stop: пустой inventory → orphan-медиа запрещён.
    if not known:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: qB не вернул путей валидных раздач — "
            "проверьте master и mount /anilibria",
            "warning",
        )
        scan_orphans = False
        if apply:
            _add_log(
                db,
                job_id,
                "orphan_cleanup: APPLY orphan-медиа ОТМЕНЁН — пустой inventory "
                "(мусор и пустые папки всё ещё можно чистить)",
                "error",
            )
    # Hard-stop: любой сбой torrents_files — список known неполный (в т.ч. без строк в БД).
    elif failed_n:
        _add_log(
            db,
            job_id,
            f"orphan_cleanup: orphan-медиа ПРОПУЩЕН — failed_hashes={failed_n} "
            f"(частичный сбой torrents_files; protected_paths={protected}). "
            "Мусор и пустые папки можно чистить.",
            "warning" if not apply else "error",
        )
        scan_orphans = False

    orphans: list[Path] = []
    if scan_orphans:
        if is_stop_requested(db, job_id):
            _add_log(db, job_id, "orphan_cleanup: остановка по запросу", "warning")
            raise JobStopRequested()
        orphans = find_orphan_files(media_root=media_root, known=known)
    if is_stop_requested(db, job_id):
        _add_log(db, job_id, "orphan_cleanup: остановка по запросу", "warning")
        raise JobStopRequested()
    junk_files = find_junk_files(media_root=media_root)
    junk_dirs = find_junk_dirs(media_root=media_root)
    # В dry-run: текущие пустые; после apply пустых станет больше (после удаления junk/orphan).
    empty_dirs = find_empty_dirs(media_root=media_root)

    _add_log(
        db,
        job_id,
        f"orphan_cleanup: orphan={len(orphans)}, junk_files={len(junk_files)}, "
        f"junk_dirs={len(junk_dirs)}, empty_dirs={len(empty_dirs)}",
    )
    orphan_bytes = paths_total_size(orphans)
    junk_bytes = paths_total_size(junk_files) + paths_total_size(junk_dirs)
    removable_bytes = orphan_bytes + junk_bytes
    _add_log(
        db,
        job_id,
        f"orphan_cleanup: размер кандидатов={format_file_size(removable_bytes)} "
        f"(orphan={format_file_size(orphan_bytes)}, junk={format_file_size(junk_bytes)})",
    )
    _log_preview(db, job_id, "orphan", orphans)
    _log_preview(db, job_id, "junk", junk_files)
    _log_preview(db, job_id, "junk_dir", junk_dirs)
    _log_preview(db, job_id, "empty_dir", empty_dirs)

    if not apply:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: dry-run — удаление не выполнялось "
            f"(кандидаты {format_file_size(removable_bytes)}; "
            "передайте apply=true и dry_run=false для удаления)",
        )
        return

    ok, detail = media_root_writable_status(media_root)
    if not ok:
        _add_log(db, job_id, f"orphan_cleanup: проверка записи: {detail}", "error")
        raise RuntimeError(
            "orphan_cleanup apply: /anilibria недоступен для записи "
            f"({detail}). Проверьте mount :rw у api/worker и recreate контейнеров."
        )

    def check_stop() -> None:
        if is_stop_requested(db, job_id):
            _add_log(db, job_id, "orphan_cleanup: остановка по запросу", "warning")
            raise JobStopRequested()

    deleted_orphans, err_orphans = _delete_files(orphans, log_fn=apply_log, check_stop=check_stop)
    deleted_junk, err_junk = _delete_files(junk_files, log_fn=apply_log, check_stop=check_stop)
    deleted_junk_dirs, err_junk_dirs = _delete_dirs_recursive(
        junk_dirs, log_fn=apply_log, check_stop=check_stop
    )
    deleted_empty, err_empty = _remove_empty_dirs(
        media_root, log_fn=apply_log, check_stop=check_stop
    )

    total_deleted = deleted_orphans + deleted_junk + deleted_junk_dirs + deleted_empty
    total_errors = err_orphans + err_junk + err_junk_dirs + err_empty
    _add_log(
        db,
        job_id,
        f"orphan_cleanup: удалено orphan={deleted_orphans}, junk_files={deleted_junk}, "
        f"junk_dirs={deleted_junk_dirs}, empty_dirs={deleted_empty}, ошибок={total_errors}, "
        f"размер кандидатов={format_file_size(removable_bytes)}",
    )
    if total_errors and total_deleted == 0:
        raise RuntimeError(
            f"orphan_cleanup apply: ничего не удалено (ошибок={total_errors}). "
            "Вероятно read-only mount."
        )
