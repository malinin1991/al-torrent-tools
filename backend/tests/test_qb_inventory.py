"""Тесты qB inventory / cleanup-фильтра / prune."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import CleanupRule
from app.services.qb_inventory import (
    InventoryFile,
    InventoryResult,
    prune_stale_inventory,
    upsert_torrent_files_inventory,
)
from app.services.torrent_cleanup import match_cleanup_rule


def test_match_cleanup_rule_detects_unregistered() -> None:
    rule = CleanupRule(
        name="test",
        tracker_host="tr.libria.fun",
        message_contains="Торрент не зарегистрирован",
        include_errored=True,
        delete_files=False,
        target_client="master",
        enabled=True,
    )
    torrent = SimpleNamespace(
        hash="abc",
        name="Show",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=5,
                url="http://tr.libria.fun:2710/announce",
                msg="Торрент не зарегистрирован на трекере",
            )
        ],
    )
    matched, reason, delete_files = match_cleanup_rule(torrent, [rule])
    assert matched is True
    assert reason == "tracker"
    assert delete_files is False


def test_match_cleanup_rule_skips_valid() -> None:
    rule = CleanupRule(
        name="test",
        tracker_host="tr.libria.fun",
        message_contains="Торрент не зарегистрирован",
        include_errored=True,
        delete_files=False,
        target_client="master",
        enabled=True,
    )
    torrent = SimpleNamespace(
        hash="abc",
        name="Show",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=2,
                url="http://tr.libria.fun:2710/announce",
                msg="",
            )
        ],
    )
    matched, reason, _ = match_cleanup_rule(torrent, [rule])
    assert matched is False
    assert reason == ""


def test_prune_stale_inventory_removes_unknown_paths(monkeypatch, tmp_path: Path) -> None:
    media_root = tmp_path / "anilibria"
    media_root.mkdir()
    known = media_root / "Show" / "ep01.mkv"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"ok")
    stale = media_root / "orphan.mkv"
    stale.write_bytes(b"x")

    monkeypatch.setattr("app.services.qb_inventory.resolve_media_root", lambda: media_root)

    keep_tf = SimpleNamespace(
        info_hash="a" * 40,
        relative_path="Show/ep01.mkv",
        full_path=str(known.resolve()),
    )
    stale_tf = SimpleNamespace(
        info_hash="b" * 40,
        relative_path="orphan.mkv",
        full_path=str(stale.resolve()),
    )
    keep_dh = SimpleNamespace(full_path=str(known.resolve()))
    stale_dh = SimpleNamespace(full_path=str(stale.resolve()))

    tf_rows = [keep_tf, stale_tf]
    dh_rows = [keep_dh, stale_dh]
    call_n = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    def fake_scalars(_stmt):
        call_n["n"] += 1
        # 1) TorrentFile, 2) archive protect для кандидатов, 3) DiskFileHash
        if call_n["n"] == 1:
            return FakeScalars(tf_rows)
        if call_n["n"] == 2:
            return FakeScalars([])  # stale hash b не в архиве → удаляем
        return FakeScalars(dh_rows)

    deleted: list[object] = []
    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.delete.side_effect = lambda obj: deleted.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=1,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=2,
                file_index=0,
                selected=True,
                full_path=str(known.resolve()),
                folder_key=str(known.parent.resolve()),
            )
        ],
    )

    pruned = prune_stale_inventory(db, inventory)

    assert pruned["torrent_files"] == 1
    assert pruned["disk_hashes"] == 1
    assert pruned["skipped"] == 0
    assert stale_tf in deleted
    assert stale_dh in deleted
    assert keep_tf not in deleted
    assert keep_dh not in deleted


def test_prune_keeps_torrent_files_for_archived_hashes(monkeypatch, tmp_path: Path) -> None:
    """Состав archived/superseded торрента не сносится prune."""
    media_root = tmp_path / "anilibria"
    media_root.mkdir()
    known = media_root / "Show" / "ep01.mkv"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"ok")
    hist = media_root / "Show" / "old.mkv"
    hist.write_bytes(b"old")

    monkeypatch.setattr("app.services.qb_inventory.resolve_media_root", lambda: media_root)

    keep_tf = SimpleNamespace(
        info_hash="a" * 40,
        relative_path="Show/ep01.mkv",
        full_path=str(known.resolve()),
    )
    history_tf = SimpleNamespace(
        info_hash="c" * 40,
        relative_path="Show/old.mkv",
        full_path=str(hist.resolve()),
    )
    call_n = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    def fake_scalars(_stmt):
        call_n["n"] += 1
        # 1) TorrentFile, 2) archive protect кандидатов, 3) DiskFileHash
        if call_n["n"] == 1:
            return FakeScalars([keep_tf, history_tf])
        if call_n["n"] == 2:
            return FakeScalars(["c" * 40])  # history hash в архиве
        return FakeScalars([])

    deleted: list[object] = []
    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.delete.side_effect = lambda obj: deleted.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=1,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=2,
                file_index=0,
                selected=True,
                full_path=str(known.resolve()),
                folder_key=str(known.parent.resolve()),
            )
        ],
    )

    pruned = prune_stale_inventory(db, inventory)
    assert pruned["torrent_files"] == 0
    assert history_tf not in deleted
    assert keep_tf not in deleted


def test_upsert_inventory_initial_status_by_prior_version() -> None:
    """Новые строки inventory: «новый» только если файла не было в прошлой версии."""
    from app.db.models import TorrentFile

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    calls = {"n": 0}

    def fake_scalars(_stmt):
        calls["n"] += 1
        # 1) существующие TorrentFile текущей версии → нет,
        # 2) состав прошлой версии (_prior_version_paths) → old.mkv уже был.
        if calls["n"] == 1:
            return FakeScalars([])
        return FakeScalars(["Show/old.mkv"])

    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.scalar.return_value = "bb" + "b" * 38  # есть прошлая версия
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/old.mkv",  # был в прошлой версии → ok
                size=1,
                file_index=0,
                selected=True,
                full_path="/m/Show/old.mkv",
                folder_key="/m/Show",
            ),
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/new.mkv",  # новый → new
                size=1,
                file_index=1,
                selected=True,
                full_path="/m/Show/new.mkv",
                folder_key="/m/Show",
            ),
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert rows["Show/old.mkv"].ui_status == "ok"
    assert rows["Show/new.mkv"].ui_status == "new"


def test_upsert_inventory_first_release_all_ok(tmp_path: Path) -> None:
    """Нет прошлой версии + готовый файл на диске → inventory сразу ok."""
    from app.db.models import TorrentFile

    class FakeScalars:
        def all(self):
            return []

    media = tmp_path / "anilibria" / "Show"
    media.mkdir(parents=True)
    complete = media / "ep01.mkv"
    complete.write_bytes(b"done")

    db = MagicMock()
    db.scalars.return_value = FakeScalars()
    db.scalar.return_value = None  # прошлой версии нет
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(complete),
                folder_key=str(media),
            )
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert rows[0].ui_status == "ok"


def test_upsert_inventory_mixed_baseline_new_episode_is_new(tmp_path: Path) -> None:
    """В составе уже ok, новый complete без хеша → inventory ставит new."""
    from app.db.models import TorrentFile

    media = tmp_path / "anilibria" / "Show"
    media.mkdir(parents=True)
    old = media / "ep01.mkv"
    newbie = media / "ep02.mkv"
    old.write_bytes(b"old")
    newbie.write_bytes(b"new")

    existing = SimpleNamespace(
        relative_path="Show/ep01.mkv",
        torrent_id=5,
        release_id=10,
        size=1,
        file_index=0,
        selected=True,
        full_path=str(old),
        ui_status="ok",
    )

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    calls = {"n": 0}

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeScalars([existing])  # current torrent_files
        return FakeScalars([])  # prior candidates / hashed paths

    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.scalar.return_value = None  # prior нет
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(old),
                folder_key=str(media),
            ),
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep02.mkv",
                size=2,
                file_index=1,
                selected=True,
                full_path=str(newbie),
                folder_key=str(media),
            ),
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = {r.relative_path: r for r in created if isinstance(r, TorrentFile)}
    assert "Show/ep01.mkv" not in rows  # уже был — не add
    assert rows["Show/ep02.mkv"].ui_status == "new"


def test_upsert_inventory_first_release_incomplete_is_new(tmp_path: Path) -> None:
    """Baseline + .!qB без хэша в БД → new (первая закачка)."""
    from app.db.models import TorrentFile

    class FakeScalars:
        def all(self):
            return []

    media = tmp_path / "anilibria" / "Show"
    media.mkdir(parents=True)
    complete = media / "ep01.mkv"
    incomplete = Path(str(complete) + ".!qB")
    incomplete.write_bytes(b"partial")

    db = MagicMock()
    db.scalars.return_value = FakeScalars()
    db.scalar.return_value = None
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(complete),
                folder_key=str(media),
            )
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert len(rows) == 1
    assert rows[0].ui_status == "new"


def test_upsert_inventory_incomplete_with_prior_hash_is_ok(tmp_path: Path) -> None:
    """Baseline + .!qB, но в disk_file_hashes уже есть хэш без суффикса → ok (не new)."""
    from app.db.models import TorrentFile

    media = tmp_path / "anilibria" / "Show"
    media.mkdir(parents=True)
    complete = media / "ep01.mkv"
    Path(str(complete) + ".!qB").write_bytes(b"partial")

    db = MagicMock()
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    calls = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    def fake_scalars(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeScalars([])  # existing torrent_files
        if calls["n"] == 2:
            return FakeScalars([])  # prior candidates
        # hashed canonical
        return FakeScalars([str(complete)])

    db.scalars.side_effect = fake_scalars
    db.scalar.return_value = None

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(complete),
                folder_key=str(media),
            )
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert len(rows) == 1
    assert rows[0].ui_status == "ok"


def test_upsert_inventory_first_release_complete_file_ok(tmp_path: Path) -> None:
    """Baseline + реальный файл на диске (без .!qB) → ok."""
    from app.db.models import TorrentFile

    class FakeScalars:
        def all(self):
            return []

    media = tmp_path / "anilibria" / "Show"
    media.mkdir(parents=True)
    complete = media / "ep01.mkv"
    complete.write_bytes(b"done")

    db = MagicMock()
    db.scalars.return_value = FakeScalars()
    db.scalar.return_value = None
    created: list[object] = []
    db.add.side_effect = lambda obj: created.append(obj)

    inventory = InventoryResult(
        valid_hashes={"a" * 40},
        files=[
            InventoryFile(
                info_hash="a" * 40,
                torrent_id=5,
                release_id=10,
                relative_path="Show/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(complete),
                folder_key=str(media),
            )
        ],
    )

    upsert_torrent_files_inventory(db, inventory)
    rows = [r for r in created if isinstance(r, TorrentFile)]
    assert rows[0].ui_status == "ok"


def test_load_cleanup_rules_filters_slave_only() -> None:
    from app.services.qb_inventory import load_cleanup_rules

    master_rule = CleanupRule(
        name="m",
        tracker_host="tr.libria.fun",
        message_contains="Торрент не зарегистрирован",
        include_errored=True,
        delete_files=False,
        target_client="master",
        enabled=True,
    )
    slave_rule = CleanupRule(
        name="s",
        tracker_host="tr.libria.fun",
        message_contains="Торрент не зарегистрирован",
        include_errored=True,
        delete_files=False,
        target_client="slave",
        enabled=True,
    )
    both_rule = CleanupRule(
        name="b",
        tracker_host="tr.libria.fun",
        message_contains="Торрент не зарегистрирован",
        include_errored=True,
        delete_files=False,
        target_client="both",
        enabled=True,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [master_rule, slave_rule, both_rule]
    rules = load_cleanup_rules(db)
    assert master_rule in rules
    assert both_rule in rules
    assert slave_rule not in rules


def test_build_inventory_skips_valid_hash_when_torrents_files_fails(monkeypatch) -> None:
    from pathlib import Path

    from app.services import qb_inventory as mod

    media_root = Path("/anilibria")
    torrent = SimpleNamespace(
        hash="a" * 40,
        name="Show",
        save_path=str(media_root / "Show"),
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[],
    )
    qb = MagicMock()
    qb.torrents_info.return_value = [torrent]
    qb.torrents_files.side_effect = RuntimeError("qB timeout")

    monkeypatch.setattr(mod, "resolve_media_root", lambda: media_root)
    monkeypatch.setattr(mod, "is_under_media_root", lambda path, **_: True)
    monkeypatch.setattr(mod, "load_cleanup_rules", lambda _db: [])
    monkeypatch.setattr(mod, "enrich_with_trackers", lambda _qb, torrents: list(torrents))
    monkeypatch.setattr(mod, "archive_meta_by_hash", lambda _db, _h: {})
    monkeypatch.setattr(mod, "extract_qb_save_path", lambda t: t.save_path)
    monkeypatch.setattr(mod, "extract_qb_torrent_hash", lambda t: t.hash)

    result = mod.build_inventory(MagicMock(), qb)
    assert "a" * 40 not in result.valid_hashes
    assert "a" * 40 in result.failed_hashes
    assert result.files == []


def test_prune_stale_inventory_keeps_failed_hashes(monkeypatch, tmp_path: Path) -> None:
    """Временный сбой torrents_files не должен сносить torrent_files этой раздачи."""
    media_root = tmp_path / "anilibria"
    media_root.mkdir()
    keep = media_root / "Show" / "ep01.mkv"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"ok")
    other = media_root / "Other" / "ep01.mkv"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")

    monkeypatch.setattr("app.services.qb_inventory.resolve_media_root", lambda: media_root)

    failed_hash = "a" * 40
    valid_hash = "b" * 40
    keep_failed_tf = SimpleNamespace(
        info_hash=failed_hash,
        relative_path="Show/ep01.mkv",
        full_path=str(keep.resolve()),
    )
    keep_valid_tf = SimpleNamespace(
        info_hash=valid_hash,
        relative_path="Other/ep01.mkv",
        full_path=str(other.resolve()),
    )
    stale_tf = SimpleNamespace(
        info_hash="c" * 40,
        relative_path="gone.mkv",
        full_path=str((media_root / "gone.mkv").resolve()),
    )
    keep_failed_dh = SimpleNamespace(full_path=str(keep.resolve()))
    keep_valid_dh = SimpleNamespace(full_path=str(other.resolve()))
    stale_dh = SimpleNamespace(full_path=str((media_root / "gone.mkv").resolve()))

    tf_rows = [keep_failed_tf, keep_valid_tf, stale_tf]
    dh_rows = [keep_failed_dh, keep_valid_dh, stale_dh]
    call_n = {"n": 0}

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    def fake_scalars(_stmt):
        call_n["n"] += 1
        # 1) TorrentFile, 2) archive protect кандидатов (c), 3) DiskFileHash
        if call_n["n"] == 1:
            return FakeScalars(tf_rows)
        if call_n["n"] == 2:
            return FakeScalars([])  # stale c не в архиве
        return FakeScalars(dh_rows)

    deleted: list[object] = []
    db = MagicMock()
    db.scalars.side_effect = fake_scalars
    db.delete.side_effect = lambda obj: deleted.append(obj)

    inventory = InventoryResult(
        valid_hashes={valid_hash},
        failed_hashes={failed_hash},
        files=[
            InventoryFile(
                info_hash=valid_hash,
                torrent_id=1,
                release_id=1,
                relative_path="Other/ep01.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(other.resolve()),
                folder_key=str(other.parent.resolve()),
            )
        ],
    )
    pruned = prune_stale_inventory(db, inventory)
    assert pruned["skipped"] == 0
    assert keep_failed_tf not in deleted
    assert keep_valid_tf not in deleted
    assert stale_tf in deleted
    assert keep_failed_dh not in deleted
    assert keep_valid_dh not in deleted
    assert stale_dh in deleted


def test_hash_backfill_clears_checkpoint_after_success(monkeypatch) -> None:
    import asyncio
    from pathlib import Path

    from app.jobs import hash_backfill as mod

    calls: dict[str, list[str]] = {"checkpoint": []}
    db = MagicMock()

    monkeypatch.setattr(mod, "resolve_media_root", lambda: Path("/anilibria"))
    monkeypatch.setattr(mod, "connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        mod,
        "build_inventory",
        lambda *_a, **_k: InventoryResult(valid_hashes={"a" * 40}, files=[]),
    )
    monkeypatch.setattr(mod, "upsert_torrent_files_inventory", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        mod,
        "prune_stale_inventory",
        lambda *_a, **_k: {"torrent_files": 0, "disk_hashes": 0, "skipped": 0},
    )
    monkeypatch.setattr(mod, "_add_log", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_get_checkpoint", lambda _db: "zzz")
    monkeypatch.setattr(mod, "_set_checkpoint", lambda _db, value: calls["checkpoint"].append(value))
    monkeypatch.setattr(mod, "_touch_job", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "get_setting_value", lambda *_a, **_k: "3")
    monkeypatch.setattr(mod, "clamp_hash_workers", lambda *_a, **_k: 3)

    asyncio.run(mod.run_hash_backfill(db, 1, {}))
    assert calls["checkpoint"][-1] == ""


def test_resolve_full_path_dedupes_save_path_equals_torrent_root(tmp_path: Path) -> None:
    """qB: save_path уже = папка шоу, name всё ещё Show/ep.mkv — без двойного пути."""
    from app.services.torrent_files_meta import resolve_full_path

    media_root = tmp_path / "anilibria"
    show = media_root / "2012" / "Sankarea [BD-Rip] [720p]"
    ep = show / "Sankarea_[01].mkv"
    ep.parent.mkdir(parents=True)
    ep.write_bytes(b"x")

    save_path = str(show)
    rel = "Sankarea [BD-Rip] [720p]/Sankarea_[01].mkv"
    resolved = resolve_full_path(save_path, rel, media_root=media_root)
    assert resolved is not None
    assert resolved.resolve() == ep.resolve()
    assert "Sankarea [BD-Rip] [720p]/Sankarea [BD-Rip] [720p]" not in str(resolved)


def test_resolve_full_path_parent_save_path(tmp_path: Path) -> None:
    from app.services.torrent_files_meta import resolve_full_path

    media_root = tmp_path / "anilibria"
    year = media_root / "2012"
    show = year / "Show"
    ep = show / "ep01.mkv"
    ep.parent.mkdir(parents=True)
    ep.write_bytes(b"x")

    resolved = resolve_full_path(str(year), "Show/ep01.mkv", media_root=media_root)
    assert resolved is not None
    assert resolved.resolve() == ep.resolve()


def test_orphan_known_includes_deduped_paths(monkeypatch, tmp_path: Path) -> None:
    """Файлы на диске не считаются orphan, если save_path = корневая папка торрента."""
    from app.jobs.orphan_cleanup import find_orphan_files
    from app.services import qb_inventory as mod

    media_root = tmp_path / "anilibria"
    show = media_root / "2012" / "Sankarea [BD-Rip] [720p]"
    files = [
        show / "Sankarea_[01].mkv",
        show / "Sankarea_[02].mkv",
    ]
    show.mkdir(parents=True)
    for f in files:
        f.write_bytes(b"ok")
    orphan = media_root / "2012" / "orphan.mkv"
    orphan.write_bytes(b"x")

    info_hash = "a" * 40
    torrent = SimpleNamespace(
        hash=info_hash,
        name="Sankarea",
        save_path=str(show),
        content_path=str(show),
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[],
    )
    qb_files = [
        SimpleNamespace(
            name=f"Sankarea [BD-Rip] [720p]/{f.name}",
            index=i,
            size=2,
            priority=1,
        )
        for i, f in enumerate(files)
    ]
    qb = MagicMock()
    qb.torrents_info.return_value = [torrent]
    qb.torrents_files.return_value = qb_files

    monkeypatch.setattr(mod, "resolve_media_root", lambda: media_root)
    monkeypatch.setattr(mod, "load_cleanup_rules", lambda _db: [])
    monkeypatch.setattr(mod, "enrich_with_trackers", lambda _qb, torrents: list(torrents))
    monkeypatch.setattr(mod, "archive_meta_by_hash", lambda _db, _h: {info_hash: (1, 1)})

    inventory = mod.build_inventory(MagicMock(), qb)
    known = {Path(item.full_path).resolve() for item in inventory.files}
    assert files[0].resolve() in known
    assert files[1].resolve() in known

    orphans = find_orphan_files(media_root=media_root, known=known)
    assert orphan.resolve() in orphans
    assert files[0].resolve() not in orphans
    assert files[1].resolve() not in orphans


def test_clamp_hash_workers() -> None:
    from app.services.file_hasher import clamp_hash_workers, normalize_file_hash_workers_setting

    assert clamp_hash_workers(3) == 3
    assert clamp_hash_workers(0) == 1
    assert clamp_hash_workers(99) == 8
    assert clamp_hash_workers("4") == 4
    assert clamp_hash_workers("nope") == 3
    assert normalize_file_hash_workers_setting("999") == "8"
    assert normalize_file_hash_workers_setting("bad") == "3"


def test_resolve_full_path_missing_file_prefers_deduped(tmp_path: Path) -> None:
    """Без файла на диске — не удваиваем папку (parent dir существует)."""
    from app.services.torrent_files_meta import resolve_full_path

    media_root = tmp_path / "anilibria"
    show = media_root / "2012" / "Sankarea [BD-Rip] [720p]"
    show.mkdir(parents=True)
    expected = show / "Sankarea_[01].mkv"

    resolved = resolve_full_path(
        str(show),
        "Sankarea [BD-Rip] [720p]/Sankarea_[01].mkv",
        media_root=media_root,
    )
    assert resolved is not None
    assert resolved.resolve() == expected.resolve()
    assert resolved.parent == show.resolve()


def test_hash_paths_parallel_isolates_errors(tmp_path: Path, monkeypatch) -> None:
    from app.services import file_hasher as mod

    good = tmp_path / "good.mkv"
    bad = tmp_path / "bad.mkv"
    good.write_bytes(b"ok-data")
    bad.write_bytes(b"bad-data")

    db = MagicMock()
    db.scalar.return_value = None
    added: list[object] = []
    db.add.side_effect = lambda obj: added.append(obj)

    real_hash = mod.hash_file_blake3

    def flaky_hash(path, **kwargs):
        if Path(path).name == "bad.mkv":
            raise OSError("I/O error")
        return real_hash(path, **kwargs)

    monkeypatch.setattr(mod, "hash_file_blake3", flaky_hash)

    stats = mod.hash_paths_parallel(db, [good, bad], workers=2)
    assert stats["hashed"] == 1
    assert stats["errors"] == 1
    assert stats["gated"] == 0
    assert len(added) == 1


def test_hash_paths_parallel_workers_hash_multiple(tmp_path: Path) -> None:
    from app.services.file_hasher import hash_paths_parallel

    files = []
    for i in range(4):
        p = tmp_path / f"f{i}.mkv"
        p.write_bytes(f"content-{i}".encode())
        files.append(p)

    db = MagicMock()
    db.scalar.return_value = None
    added: list[object] = []
    db.add.side_effect = lambda obj: added.append(obj)

    stats = hash_paths_parallel(db, files, workers=3)
    assert stats["hashed"] == 4
    assert stats["errors"] == 0
    assert stats["gated"] == 0
    assert stats["progress_index"] == 4
    assert len(added) == 4
    hashes = {row.content_hash for row in added}
    assert len(hashes) == 4


def test_hash_paths_parallel_progress_index_includes_errors(tmp_path: Path, monkeypatch) -> None:
    from app.services import file_hasher as mod

    good = tmp_path / "good.mkv"
    bad = tmp_path / "bad.mkv"
    good.write_bytes(b"ok")
    bad.write_bytes(b"bad")
    db = MagicMock()
    db.scalar.return_value = None
    db.add.side_effect = lambda obj: None

    real_hash = mod.hash_file_blake3

    def flaky(path, **kwargs):
        if Path(path).name == "bad.mkv":
            raise OSError("boom")
        return real_hash(path, **kwargs)

    monkeypatch.setattr(mod, "hash_file_blake3", flaky)
    stats = mod.hash_paths_parallel(db, [good, bad], workers=1, progress_start=10)
    assert stats["hashed"] == 1
    assert stats["errors"] == 1
    assert stats["progress_index"] == 12  # start + both attempts
    assert stats["stopped"] == 0


def test_hash_paths_parallel_stops_between_files(tmp_path: Path) -> None:
    """Stop: текущий файл дожимается, следующие не стартуют; hashed сохранён."""
    from app.services.file_hasher import hash_paths_parallel

    files = []
    for i in range(5):
        p = tmp_path / f"f{i}.mkv"
        p.write_bytes(f"content-{i}".encode())
        files.append(p)

    db = MagicMock()
    db.scalar.return_value = None
    added: list[object] = []
    db.add.side_effect = lambda obj: added.append(obj)

    def should_stop() -> bool:
        return len(added) >= 1

    stats = hash_paths_parallel(db, files, workers=1, should_stop=should_stop)
    assert stats["stopped"] == 1
    assert stats["hashed"] == 1
    assert len(added) == 1


def test_hash_paths_parallel_stop_waits_inflight_workers(tmp_path: Path, monkeypatch) -> None:
    """При stop in-flight воркеры дожимают файл, новые задачи не ставятся."""
    import concurrent.futures
    import threading

    from app.services import file_hasher as mod

    files = []
    for i in range(6):
        p = tmp_path / f"f{i}.mkv"
        p.write_bytes(f"x{i}".encode())
        files.append(p)

    db = MagicMock()
    db.scalar.return_value = None
    added: list[object] = []
    db.add.side_effect = lambda obj: added.append(obj)

    started = threading.Event()
    release = threading.Event()
    stop_flag = {"v": False}
    active = {"n": 0}
    lock = threading.Lock()

    real_hash = mod.hash_file_blake3

    def slow_hash(path, **kwargs):
        with lock:
            active["n"] += 1
            if active["n"] >= 2:
                started.set()
        assert release.wait(timeout=5)
        try:
            return real_hash(path, **kwargs)
        finally:
            with lock:
                active["n"] -= 1

    monkeypatch.setattr(mod, "hash_file_blake3", slow_hash)

    def runner():
        return mod.hash_paths_parallel(
            db,
            files,
            workers=2,
            should_stop=lambda: stop_flag["v"],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(runner)
        assert started.wait(timeout=5)
        stop_flag["v"] = True
        release.set()
        stats = fut.result(timeout=10)

    assert stats["stopped"] == 1
    # Стартовали ≤2 (окно воркеров), новые после stop не брались.
    assert 1 <= stats["hashed"] <= 2
    assert len(added) == stats["hashed"]
