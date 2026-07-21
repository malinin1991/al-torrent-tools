"""Поиск orphan-файлов под /anilibria (dry-run по умолчанию)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog
from app.services.qb_inventory import build_inventory, connect_master
from app.services.torrent_files_meta import (
    QB_INCOMPLETE_SUFFIX,
    is_under_media_root,
    resolve_media_root,
)

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi", ".m2ts", ".ts"}


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def media_root_is_writable(media_root: Path) -> bool:
    """Проверка, что apply сможет удалять файлы (не read-only mount)."""
    probe = media_root / ".altt_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            if probe.exists():
                probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


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

    # Те же валидные раздачи, что и hash_backfill (без «не зарегистрирован»).
    inventory = build_inventory(db, qb, log_fn=log_fn)
    known = {Path(item.full_path).resolve() for item in inventory.files}
    _add_log(
        db,
        job_id,
        f"orphan_cleanup: валидных раздач={len(inventory.valid_hashes)}, "
        f"известных путей={len(known)}, invalid_skipped={inventory.skipped_invalid}",
    )

    # Hard-stop: пустой inventory → apply запрещён (иначе снесём всю медиатеку).
    if not known:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: qB не вернул путей валидных раздач — "
            "проверьте master и mount /anilibria",
            "warning",
        )
        if apply:
            apply = False
            _add_log(
                db,
                job_id,
                "orphan_cleanup: APPLY ОТМЕНЁН — отказ удалять при пустом inventory",
                "error",
            )
            # Не считаем orphan’ов как «всё на диске» — слишком опасно даже для лога.
            _add_log(db, job_id, "orphan_cleanup: сканирование ФС пропущено (защита)")
            return

    orphans = find_orphan_files(media_root=media_root, known=known)
    _add_log(db, job_id, f"orphan_cleanup: найдено orphan={len(orphans)}")

    preview = orphans[:100]
    for path in preview:
        _add_log(db, job_id, f"orphan: {path}")
    if len(orphans) > len(preview):
        _add_log(db, job_id, f"orphan_cleanup: … ещё {len(orphans) - len(preview)} файлов")

    deleted = 0
    if apply:
        if not media_root_is_writable(media_root):
            raise RuntimeError(
                "orphan_cleanup apply: /anilibria недоступен для записи "
                "(проверьте mount rw у api/worker). Удаление отменено."
            )
        errors = 0
        for path in orphans:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                errors += 1
                _add_log(db, job_id, f"orphan_cleanup: не удалось удалить {path}: {exc}", "error")
        _add_log(db, job_id, f"orphan_cleanup: удалено={deleted}, ошибок={errors}")
        if errors and deleted == 0:
            raise RuntimeError(
                f"orphan_cleanup apply: ни один файл не удалён (ошибок={errors}). "
                "Вероятно read-only mount."
            )
    else:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: dry-run — удаление не выполнялось "
            "(передайте apply=true и dry_run=false для удаления)",
        )
