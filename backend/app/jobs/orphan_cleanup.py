"""Поиск orphan-файлов под /anilibria (dry-run по умолчанию)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import JobLog, TorrentArchive, TorrentFile
from app.services.torrent_files_meta import (
    QB_INCOMPLETE_SUFFIX,
    is_under_media_root,
    resolve_media_root,
)

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi", ".m2ts", ".ts"}


def _add_log(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message))
    db.commit()


def collect_known_paths(db: Session) -> set[Path]:
    """Пути из torrent_files для api_present торрентов."""
    present_hashes = set(
        db.scalars(
            select(TorrentArchive.info_hash).where(TorrentArchive.api_present.is_(True))
        ).all()
    )
    present_hashes = {(h or "").strip().lower() for h in present_hashes if h}
    if not present_hashes:
        return set()
    rows = db.scalars(
        select(TorrentFile.full_path).where(
            TorrentFile.info_hash.in_(present_hashes),
            TorrentFile.full_path.isnot(None),
        )
    ).all()
    return {Path(p).resolve() for p in rows if p}


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

    known = collect_known_paths(db)
    _add_log(db, job_id, f"orphan_cleanup: известных путей из torrent_files={len(known)}")
    orphans = find_orphan_files(media_root=media_root, known=known)
    _add_log(db, job_id, f"orphan_cleanup: найдено orphan={len(orphans)}")

    # Ограничим лог первыми N
    preview = orphans[:100]
    for path in preview:
        _add_log(db, job_id, f"orphan: {path}")
    if len(orphans) > len(preview):
        _add_log(db, job_id, f"orphan_cleanup: … ещё {len(orphans) - len(preview)} файлов")

    deleted = 0
    if apply:
        for path in orphans:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                _add_log(db, job_id, f"orphan_cleanup: не удалось удалить {path}: {exc}", "error")
        _add_log(db, job_id, f"orphan_cleanup: удалено={deleted}")
    else:
        _add_log(
            db,
            job_id,
            "orphan_cleanup: dry-run — удаление не выполнялось "
            "(передайте apply=true и dry_run=false для удаления)",
        )
