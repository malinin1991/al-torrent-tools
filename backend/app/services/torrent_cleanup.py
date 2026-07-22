from types import SimpleNamespace
from typing import Any

import qbittorrentapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CleanupRule, JobLog, QbClient
from app.services.job_runner import JobStopRequested, is_stop_requested

# qB 5+: TrackerError — ответ трекера с ошибкой (в т.ч. «не зарегистрирован»).
_TRACKER_ERROR_STATUS = 5


def _tracker_list(trackers: Any) -> list[Any]:
    """Нормализует ответ qbittorrent-api: List / .data / обычный list."""
    if trackers is None:
        return []
    # Обычный list/TrackersList: итерируем напрямую.
    # Старый скрипт использовал torrent.trackers.data — учитываем AttrDict.
    if isinstance(trackers, list):
        return list(trackers)
    data = getattr(trackers, "data", None)
    if data is not None:
        return list(data)
    try:
        return list(trackers)
    except TypeError:
        return []


def _tracker_url(tracker: Any) -> str:
    return str(getattr(tracker, "url", "") or "").casefold()


def _tracker_message(tracker: Any) -> str:
    return str(
        getattr(tracker, "msg", None) or getattr(tracker, "message", None) or ""
    ).casefold()


def _tracker_status(tracker: Any) -> int:
    try:
        return int(getattr(tracker, "status", -1))
    except (TypeError, ValueError):
        return -1


def _tracker_match_entries(tracker: Any) -> list[Any]:
    """Сам трекер + endpoints (qB 5 может держать msg на endpoint)."""
    entries = [tracker]
    endpoints = getattr(tracker, "endpoints", None)
    if not endpoints:
        return entries
    try:
        entries.extend(list(endpoints))
    except TypeError:
        data = getattr(endpoints, "data", None)
        if data is not None:
            entries.extend(list(data))
    return entries


def _tracker_matches_rule(tracker: Any, rule: CleanupRule) -> bool:
    """host в url + status=TrackerError(5) + текст в msg."""
    host = (rule.tracker_host or "").strip().casefold()
    needle = (rule.message_contains or "").strip().casefold()
    if not host or not needle:
        return False
    if host not in _tracker_url(tracker):
        return False

    for entry in _tracker_match_entries(tracker):
        if _tracker_status(entry) != _TRACKER_ERROR_STATUS:
            continue
        if needle in _tracker_message(entry):
            return True
    return False


def _tracker_host_present(trackers: list[Any], host: str) -> bool:
    """Хост трекера встречается хотя бы в одном URL (без требования status/msg)."""
    needle = (host or "").strip().casefold()
    if not needle:
        return False
    return any(needle in _tracker_url(tracker) for tracker in trackers)


def match_cleanup_rule(torrent: Any, rules: list[CleanupRule]) -> tuple[bool, str, bool]:
    """(matched, reason, delete_files) по cleanup-правилам («не зарегистрирован» и т.п.)."""
    trackers = _tracker_list(getattr(torrent, "trackers", None))
    state_enum = getattr(torrent, "state_enum", None)
    is_errored = bool(getattr(state_enum, "is_errored", False))

    matched = False
    reason = ""
    delete_files = False
    for rule in rules:
        should_remove = False
        rule_reason = ""
        for tracker in trackers:
            if _tracker_matches_rule(tracker, rule):
                should_remove = True
                rule_reason = "tracker"
                break
        if (
            not should_remove
            and rule.include_errored
            and is_errored
            and _tracker_host_present(trackers, rule.tracker_host)
        ):
            should_remove = True
            rule_reason = "errored"
        if should_remove:
            matched = True
            if not reason:
                reason = rule_reason
            if rule.delete_files:
                delete_files = True
    return matched, reason, delete_files


def find_removable_torrents(
    torrent_list: list[Any],
    rules: list[CleanupRule],
) -> list[dict[str, Any]]:
    removable: dict[str, dict[str, Any]] = {}
    for torrent in torrent_list:
        torrent_hash = str(getattr(torrent, "hash", "")).lower()
        if not torrent_hash:
            continue
        matched, reason, delete_files = match_cleanup_rule(torrent, rules)
        if not matched:
            continue
        current = removable.get(torrent_hash)
        if current is None:
            removable[torrent_hash] = {
                "hash": torrent_hash,
                "name": str(getattr(torrent, "name", torrent_hash)),
                "delete_files": delete_files,
                "reason": reason,
            }
        elif delete_files:
            current["delete_files"] = True
    return list(removable.values())


class TorrentCleanupService:
    def __init__(self, db: Session, job_id: int) -> None:
        self._db = db
        self._job_id = job_id

    def _add_log(self, message: str, level: str = "info") -> None:
        self._db.add(JobLog(job_id=self._job_id, level=level, message=message))
        self._db.commit()

    def add_log(self, message: str, level: str = "info") -> None:
        self._add_log(message=message, level=level)

    def _check_stop(self) -> None:
        if is_stop_requested(self._db, self._job_id):
            self._add_log("Cleanup: остановка по запросу", "warning")
            raise JobStopRequested()

    def _get_clients_by_target(self, target: str) -> list[QbClient]:
        query = select(QbClient).where(QbClient.enabled.is_(True))
        if target == "both":
            query = query.where(QbClient.role.in_(("master", "slave")))
        else:
            query = query.where(QbClient.role == target)
        return list(self._db.scalars(query).all())

    def _enrich_with_trackers(self, client: qbittorrentapi.Client, torrents: list[Any]) -> list[Any]:
        """torrents/info не отдаёт трекеры — подгружаем torrents_trackers по каждому hash."""
        enriched: list[Any] = []
        for torrent in torrents:
            torrent_hash = str(getattr(torrent, "hash", ""))
            try:
                trackers = list(client.torrents_trackers(torrent_hash=torrent_hash))
            except Exception as exc:  # noqa: BLE001 — лог и пропуск одного торрента
                self._add_log(
                    f"Cleanup: не удалось получить trackers для {torrent_hash}: {exc}",
                    "warning",
                )
                trackers = []
            enriched.append(
                SimpleNamespace(
                    hash=torrent_hash,
                    name=getattr(torrent, "name", torrent_hash),
                    state_enum=getattr(torrent, "state_enum", None),
                    trackers=trackers,
                )
            )
        return enriched

    def run(self, dry_run: bool = True, *, target_role: str | None = None) -> dict[str, int]:
        """Очистка qB. target_role=master|slave — только клиенты роли и правила {role, both}."""
        rules = self._db.scalars(select(CleanupRule).where(CleanupRule.enabled.is_(True))).all()
        if not rules:
            self._add_log("Cleanup: нет активных правил")
            return {"checked": 0, "matched": 0, "deleted": 0}

        if target_role is not None and target_role not in {"master", "slave"}:
            raise ValueError(f"Cleanup: неверный target_role={target_role!r}")

        checked = 0
        matched = 0
        deleted = 0
        for rule in rules:
            self._check_stop()
            if rule.target_client not in {"master", "slave", "both"}:
                self._add_log(
                    f"Cleanup: правило {rule.id} пропущено, неверный target_client={rule.target_client}",
                    "warning",
                )
                continue
            if target_role is not None and rule.target_client not in {target_role, "both"}:
                continue
            clients_target = target_role if target_role is not None else rule.target_client
            clients = self._get_clients_by_target(clients_target)
            if not clients:
                self._add_log(f"Cleanup: нет активных клиентов для правила {rule.id}")
                continue

            for db_client in clients:
                self._check_stop()
                client = qbittorrentapi.Client(
                    host=db_client.host,
                    port=db_client.port,
                    username=db_client.username,
                    password=db_client.password_encrypted,
                )
                client.auth_log_in()
                torrents = self._enrich_with_trackers(client, list(client.torrents_info()))
                checked += len(torrents)
                self._add_log(
                    f"Cleanup: клиент {db_client.role}/{db_client.name}, "
                    f"загружено торрентов {len(torrents)} для правила {rule.name} "
                    f"(host={rule.tracker_host!r}, msg={rule.message_contains!r})"
                )
                removable = find_removable_torrents(torrents, [rule])
                matched += len(removable)
                if not removable:
                    self._add_log(
                        f"Cleanup: клиент {db_client.role}/{db_client.name}, "
                        f"правило {rule.name} не нашло кандидатов"
                    )
                    continue

                self._add_log(
                    f"Cleanup: клиент {db_client.role}/{db_client.name}, "
                    f"правило {rule.name} — найдено {len(removable)} торрентов"
                )
                if dry_run:
                    for item in removable:
                        self._add_log(
                            f"Cleanup dry-run: {item['name']} ({item['hash']}) reason={item['reason']}"
                        )
                    continue

                hashes_delete_files_true = [item["hash"] for item in removable if item["delete_files"]]
                hashes_delete_files_false = [item["hash"] for item in removable if not item["delete_files"]]
                if hashes_delete_files_true:
                    self._check_stop()
                    client.torrents_delete(delete_files=True, torrent_hashes=hashes_delete_files_true)
                    deleted += len(hashes_delete_files_true)
                if hashes_delete_files_false:
                    self._check_stop()
                    client.torrents_delete(delete_files=False, torrent_hashes=hashes_delete_files_false)
                    deleted += len(hashes_delete_files_false)
        return {"checked": checked, "matched": matched, "deleted": deleted}
