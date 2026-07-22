"""Очистка ложных orphan-событий в file_change_events."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.db_maintenance import purge_false_orphan_events


def _in_ids_from_delete(stmt) -> set[int]:
    """Достаёт id из DELETE ... WHERE id IN (...)."""
    for crit in getattr(stmt, "_where_criteria", ()):
        right = getattr(crit, "right", None)
        value = getattr(right, "value", None)
        if isinstance(value, (list, tuple, set, frozenset)):
            return {int(x) for x in value}
        if value is not None:
            try:
                return {int(value)}
            except (TypeError, ValueError):
                continue
    return set()


def test_purge_false_orphan_events_removes_foreign_and_keeps_local(tmp_path: Path) -> None:
    media = tmp_path / "anilibria"
    show = media / "2012" / "Sakurasou"
    show.mkdir(parents=True)
    ep = show / "ep01.mkv"
    ep.write_bytes(b"1")
    local_orphan = show / "extra.mkv"
    local_orphan.write_bytes(b"o")
    foreign = media / "2012" / "Nekomonogatari" / "bonus.mkv"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"x")

    torrent_id = 42
    events = [
        SimpleNamespace(
            id=1,
            torrent_id=torrent_id,
            kind="orphan",
            relative_path=None,
            full_path=str(local_orphan.resolve()),
        ),
        SimpleNamespace(
            id=2,
            torrent_id=torrent_id,
            kind="orphan",
            relative_path=None,
            full_path=str(foreign.resolve()),
        ),
        SimpleNamespace(
            id=3,
            torrent_id=torrent_id,
            kind="orphan",
            relative_path=None,
            full_path=str(local_orphan.resolve()),
        ),
        SimpleNamespace(
            id=4,
            torrent_id=None,
            kind="orphan",
            relative_path=None,
            full_path=str(foreign.resolve()),
        ),
    ]
    files = [SimpleNamespace(torrent_id=torrent_id, full_path=str(ep.resolve()))]

    call_n = {"n": 0}

    def scalars_side_effect(_stmt):  # noqa: ANN001
        call_n["n"] += 1
        mock = MagicMock()
        mock.all.return_value = events if call_n["n"] == 1 else files
        return mock

    db = MagicMock()
    db.scalars.side_effect = scalars_side_effect
    captured: dict[str, set[int]] = {}

    def execute(stmt):  # noqa: ANN001
        captured["ids"] = _in_ids_from_delete(stmt)
        return MagicMock()

    db.execute.side_effect = execute

    stats = purge_false_orphan_events(db, media_root=media, commit=True)

    assert stats["orphan_events_scanned"] == 4
    # foreign(2), null torrent(4), дубликат local с меньшим id(1); оставляем local id=3
    assert captured["ids"] == {1, 2, 4}
    assert stats["orphan_events_removed"] == 2
    assert stats["orphan_duplicates_removed"] == 1
    assert stats["orphan_events_kept"] == 1
    db.commit.assert_called_once()


def test_purge_false_orphan_events_noop_when_empty() -> None:
    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    stats = purge_false_orphan_events(db, media_root=Path("/anilibria"), commit=False)
    assert stats == {
        "orphan_events_scanned": 0,
        "orphan_events_removed": 0,
        "orphan_events_kept": 0,
        "orphan_duplicates_removed": 0,
    }
    db.execute.assert_not_called()
