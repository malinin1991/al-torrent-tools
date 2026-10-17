"""Read-only HEVC work list; one pairing evaluation and page-bounded disk checks.

Readiness counters are database snapshots. Only the visible page is checked
against the filesystem. The existing encoder endpoint owns final eligibility,
idempotency and duplicate protection; a receipt never means encoding finished.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import os
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

from sqlalchemy import String, any_, bindparam, case, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import EncoderSubmission, EncoderSubmissionItem, Job, Release, TorrentArchive, TorrentFile
from app.services.encoder_history import LABELS
from app.services.hevc_pairing import (
    classify_archive_codec, file_keys_by_archive_id, find_unpaired_avc,
    overdue_hours_past_sla,
)
from app.services.releases_view import _latest_pipeline_by_hash, format_bytes
from app.services.runtime_settings import get_setting_value
from app.services.torrent_files_meta import QB_INCOMPLETE_SUFFIX, is_junk_file, resolve_media_root
from app.services.torrent_qb_meta import build_release_admin_url, build_release_torrents_url, resolve_anilibria_site_url
from app.utils.datetime_fmt import as_utc_iso, utcnow

VIEWS = {
    "all": "Все", "ready": "Готово к отправке", "overdue": "Просрочено",
    "unavailable": "Исходник недоступен", "unknown": "Приём требует проверки",
}
_VIDEO_SUFFIXES = frozenset({".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm"})
_HEVC_LABELS = {"missing": "HEVC отсутствует", "overdue": "HEVC отстаёт", "type_mismatch": "Расхождение типов"}
_BLOCKING_RECEIPTS = frozenset({"sending", "accepted", "unknown"})


class _ArchiveRow(NamedTuple):
    """Request-local values; repeated pairing reads avoid SQL Row lookups."""
    id: int
    release_id: int
    torrent_id: int
    info_hash: str
    torrent_type: str | None
    torrent_description: str | None
    quality_json: object
    created_at: datetime | None
    api_created_at: datetime | None
    api_present: bool | None
    superseded: bool | None
    ignore_hevc: bool | None
    anime_name: str | None
    release_alias: str | None


class _FileRow(NamedTuple):
    id: int | None
    info_hash: str
    relative_path: str
    full_path: str | None
    size: int | None
    selected: bool | None
    ui_status: str | None
    is_checking: bool | None
    media_present: bool | None
    updated_at: datetime | None


def _file_projection(db: Session, hashes: list[str]):
    """Keep all composition paths, materialize metadata only for active AVC.

    One SELECT retains the same file snapshot for pairing and readiness. Raw
    hash/path values are never normalized or filtered. The later Python hash
    lookup still owns candidate identity. PostgreSQL uses one array parameter;
    other dialects fall back to the original metadata for larger IN lists.
    """
    columns = (
        TorrentFile.id, TorrentFile.info_hash, TorrentFile.relative_path,
        TorrentFile.full_path, TorrentFile.size, TorrentFile.selected, TorrentFile.ui_status,
        TorrentFile.is_checking, TorrentFile.media_present, TorrentFile.updated_at,
    )
    if db.get_bind().dialect.name == "postgresql":
        active = TorrentFile.info_hash == any_(bindparam("workbench_active_hashes", hashes, type_=ARRAY(String)))
    elif len(hashes) <= 100:
        active = TorrentFile.info_hash.in_(hashes)
    else:
        return columns
    return tuple(column if column.key in {"info_hash", "relative_path"}
                 else case((active, column), else_=None).label(column.key) for column in columns)


def _chunks(values, size=500):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _latest_receipts(db: Session, hashes: list[str], now: datetime) -> dict:
    """Latest exact-source receipt; no recovery mutation during a page read."""
    result = {}
    for chunk in _chunks(hashes):
        ranked = select(
            EncoderSubmissionItem.info_hash, EncoderSubmissionItem.relative_path,
            EncoderSubmissionItem.status, EncoderSubmissionItem.submission_id,
            EncoderSubmissionItem.error, EncoderSubmission.expires_at,
            EncoderSubmission.request_started_at, EncoderSubmission.finished_at,
            func.row_number().over(
                partition_by=(EncoderSubmissionItem.info_hash, EncoderSubmissionItem.relative_path),
                order_by=EncoderSubmissionItem.id.desc(),
            ).label("rank"),
        ).join(EncoderSubmission).where(EncoderSubmissionItem.info_hash.in_(chunk)).subquery()
        for item in db.execute(select(ranked).where(ranked.c.rank == 1)):
            state = item.status
            expired = bool(state == "sending" and item.finished_at is None and item.expires_at and item.expires_at < now)
            if expired:
                state = "unknown" if item.request_started_at else "not_sent"
            result[(item.info_hash, item.relative_path)] = {
                "status": state, "label": LABELS.get(state, state),
                "submission_id": item.submission_id,
                "history_url": f"/encoder-history?submission_id={item.submission_id}",
                "error": item.error or "", "expired": expired,
            }
    return result


def _probe_visible_files(rows: list[dict], media_root: Path) -> dict[int, str | None]:
    """Only visible paths: resolve symlinks/root, then one scandir per parent.

    Missing data and failed/unknown filesystem checks fail closed. A complete
    file with a neighbouring .!qB remains valid, matching download semantics.
    """
    results = {}
    by_parent = defaultdict(list)
    for row in rows:
        fid, raw = row["file_id"], row["full_path"]
        if fid is None:
            continue
        if not raw:
            results[fid] = "Путь исходника ещё не определён"
            continue
        path = Path(raw)
        if path.name.endswith(QB_INCOMPLETE_SUFFIX):
            results[fid] = "Файл имеет неполный суффикс .!qB"
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(media_root)
        except ValueError:
            results[fid] = "Путь исходника находится вне каталога медиа"
            continue
        except (OSError, RuntimeError):
            results[fid] = "Не удалось проверить путь исходника"
            continue
        by_parent[resolved.parent].append((fid, resolved.name))
    for parent, wanted in by_parent.items():
        names = {name for _, name in wanted}
        complete = set()
        partial = set()
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.name not in names and not (entry.name.endswith(QB_INCOMPLETE_SUFFIX) and entry.name[:-len(QB_INCOMPLETE_SUFFIX)] in names):
                        continue
                    if entry.is_file(follow_symlinks=True):
                        if entry.name in names:
                            complete.add(entry.name)
                        else:
                            partial.add(entry.name[:-len(QB_INCOMPLETE_SUFFIX)])
        except FileNotFoundError:
            for fid, _ in wanted:
                results[fid] = "Исходник отсутствует на диске"
            continue
        except OSError:
            for fid, _ in wanted:
                results[fid] = "Не удалось прочитать каталог исходника"
            continue
        for fid, name in wanted:
            results[fid] = None if name in complete else "Есть только неполный файл .!qB" if name in partial else "Исходник отсутствует на диске"
    return results


def list_hevc_workbench(
    db: Session, *, view: str = "all", search: str = "", page: int = 1,
    per_page: int = 30, now: datetime | None = None,
) -> dict:
    current = now or utcnow()
    view = view if view in VIEWS else "all"
    search = str(search or "").strip()[:200]
    per_page = max(1, min(int(per_page), 100))
    # Compact rows retain historical versions: they contribute SLA anchors.
    archives = [_ArchiveRow(*row) for row in db.execute(select(
        TorrentArchive.id, TorrentArchive.release_id, TorrentArchive.torrent_id,
        TorrentArchive.info_hash, TorrentArchive.torrent_type, TorrentArchive.torrent_description,
        TorrentArchive.quality_json, TorrentArchive.created_at, TorrentArchive.api_created_at,
        TorrentArchive.api_present, TorrentArchive.superseded, TorrentArchive.ignore_hevc,
        TorrentArchive.anime_name, TorrentArchive.release_alias,
    ))]
    active = {}
    for archive in sorted(archives, key=lambda item: item.id, reverse=True):
        if not archive.api_present or archive.superseded or archive.info_hash in active:
            continue
        if classify_archive_codec(quality_json=archive.quality_json, torrent_type=archive.torrent_type) == "AVC":
            active[archive.info_hash] = archive
    files = [_FileRow(*row) for row in db.execute(select(*_file_projection(db, list(active))))]
    needs = {item.archive_id: item for item in find_unpaired_avc(
        archives, now=current, file_keys_by_archive_id=file_keys_by_archive_id(archives, files),
    )}
    receipts = _latest_receipts(db, sorted(active), current)
    hash_jobs = set()
    for params, in db.execute(select(Job.params_json).where(Job.type == "hash_torrent", Job.status.in_(("pending", "running", "stopping")))):
        if isinstance(params, dict):
            hash_jobs.add(str(params.get("info_hash") or "").strip().lower())
    release_meta = {}
    for chunk in _chunks(sorted({item.release_id for item in active.values()})):
        for item in db.execute(select(Release.release_id, Release.title, Release.release_alias, Release.source).where(Release.release_id.in_(chunk))):
            release_meta[item.release_id] = item
    by_hash = defaultdict(list)
    for file in files:
        if file.info_hash in active and not is_junk_file(file.relative_path) and Path(file.relative_path).suffix.casefold() in _VIDEO_SUFFIXES:
            by_hash[file.info_hash].append(file)
    rows = []
    for info_hash, archive in active.items():
        need = needs.get(archive.id)
        candidates = by_hash.get(info_hash, [])
        for file in candidates or [None]:
            receipt = receipts.get((info_hash, file.relative_path)) if file else None
            if need is None and not (receipt and receipt["status"] == "unknown"):
                continue
            meta = release_meta.get(archive.release_id)
            title = (meta.title if meta else None) or archive.anime_name or str(archive.release_id)
            alias = (meta.release_alias if meta else None) or archive.release_alias or ""
            relative = file.relative_path if file else "Состав видеофайлов ещё не получен"
            if search and search.casefold() not in " ".join((title, alias, str(archive.release_id), str(archive.torrent_id), relative)).casefold():
                continue
            reasons = []
            if file is None:
                reasons.append("Состав видеофайлов ещё не получен")
            else:
                if not file.selected:
                    reasons.append("Файл не выбран на master")
                if (file.ui_status or "").strip().lower() not in {"new", "changed", "ok"}:
                    reasons.append("Исторический статус не разрешает отправку")
                if file.is_checking:
                    reasons.append("По последней проверке файл ещё скачивается или проверяется")
                if info_hash in hash_jobs:
                    reasons.append("Выполняется или ожидает хеширование исходника")
                if not file.full_path:
                    reasons.append("Путь исходника ещё не определён")
                elif file.full_path.endswith(QB_INCOMPLETE_SUFFIX):
                    reasons.append("Файл имеет неполный суффикс .!qB")
                if not file.media_present:
                    reasons.append("По последней проверке полного файла на диске нет")
            db_ready = not reasons
            selectable = bool(need and db_ready and not (receipt and receipt["status"] in _BLOCKING_RECEIPTS))
            rows.append({
                "file_id": file.id if file else None, "archive_id": archive.id,
                "release_id": archive.release_id, "torrent_id": archive.torrent_id,
                "info_hash": info_hash, "relative_path": relative, "full_path": file.full_path if file else "",
                "identity": f"{info_hash}:{relative}" if file else f"archive:{archive.id}",
                "title": title, "alias": alias, "archive_title": archive.anime_name or title, "source": meta.source if meta else None,
                "size_label": format_bytes(file.size) if file else "—", "sticky_status": file.ui_status if file else "—",
                "hevc_status": need.status if need else "", "hevc_label": _HEVC_LABELS[need.status] if need else "Проблема HEVC больше не обнаружена",
                "age_hours": round(need.age_hours, 1) if need and need.age_hours is not None else None,
                "overdue_hours": round(overdue_hours_past_sla(need.age_hours), 1) if need and need.overdue and need.age_hours is not None else None,
                "age_from_api": need.age_from_api if need else False,
                "anchor_at": as_utc_iso(need.created_at) if need and need.created_at else "",
                "pair_archive_id": need.paired_hevc_archive_id if need else None,
                "episodes": archive.torrent_description or "", "torrent_type": archive.torrent_type or "",
                "receipt": receipt, "db_reasons": reasons, "reasons": list(reasons),
                "db_ready": db_ready, "preselectable": selectable, "selectable": selectable,
                "snapshot_at": as_utc_iso(file.updated_at) if file and file.updated_at else "",
                "sort_anchor": need.created_at if need and need.created_at else datetime.max,
            })
    counts = {
        "all": len(rows), "ready": sum(row["preselectable"] for row in rows),
        "overdue": sum(row["hevc_status"] == "overdue" for row in rows),
        "unavailable": sum(not row["db_ready"] for row in rows),
        "unknown": sum(bool(row["receipt"] and row["receipt"]["status"] == "unknown") for row in rows),
    }
    predicates = {
        "all": lambda row: True, "ready": lambda row: row["preselectable"],
        "overdue": lambda row: row["hevc_status"] == "overdue",
        "unavailable": lambda row: not row["db_ready"],
        "unknown": lambda row: bool(row["receipt"] and row["receipt"]["status"] == "unknown"),
    }
    filtered = [row for row in rows if predicates[view](row)]
    priority = {"overdue": 0, "missing": 1, "type_mismatch": 2, "": 3}
    filtered.sort(key=lambda row: (priority[row["hevc_status"]], row["sort_anchor"], row["release_id"], row["archive_id"], row["relative_path"], row["file_id"] or 0))
    total = len(filtered)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    visible = filtered[(page - 1) * per_page:page * per_page]
    probes = _probe_visible_files(visible, resolve_media_root()) if visible else {}
    checked_at = as_utc_iso(utcnow())
    pipelines = _latest_pipeline_by_hash(db, list({row["info_hash"] for row in visible})) if visible else {}
    admin_template = get_setting_value(db, "anilibria_admin_url_template", settings.anilibria_admin_url_template, allow_empty=True)
    site_url = resolve_anilibria_site_url()
    for row in visible:
        error = probes.get(row["file_id"])
        row["fs_checked"] = row["file_id"] in probes
        row["fs_available"] = row["fs_checked"] and error is None
        if error and error not in row["reasons"]:
            row["reasons"].append(error)
        row["selectable"] = row["preselectable"] and row["fs_available"]
        row["availability_changed"] = row["fs_checked"] and row["db_ready"] != row["fs_available"]
        row["pipeline"] = pipelines.get(row["info_hash"], (None, None, None))
        row["admin_url"] = build_release_admin_url(row["release_id"], admin_template)
        row["release_url"] = None if row["source"] == "backoffice" else build_release_torrents_url(row["alias"], site_url=site_url)
    changed = [row for row in visible if not row["selectable"]] if view == "ready" else []
    displayed = [row for row in visible if row["selectable"]] if view == "ready" else visible
    def url(path, **params):
        return path + "?" + urlencode({"view": view, "search": search, "page": page, "per_page": per_page, **params})
    return {
        "rows": displayed, "changed_rows": changed, "view": view, "views": VIEWS,
        "search": search, "counts": counts, "total": total, "page": page, "total_pages": pages, "per_page": per_page,
        "checked_at": checked_at, "checked_count": len(probes), "available_count": sum(error is None for error in probes.values()),
        "live_url": url("/hevc-workbench/live"),
        "view_urls": {key: url("/hevc-workbench", view=key, page=1) for key in VIEWS},
        "prev_url": url("/hevc-workbench", page=page - 1) if page > 1 else None,
        "next_url": url("/hevc-workbench", page=page + 1) if page < pages else None,
    }
