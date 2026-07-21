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
        # Первый select — TorrentFile, второй — DiskFileHash
        if call_n["n"] == 1:
            return FakeScalars(tf_rows)
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
    assert result.files == []

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
    monkeypatch.setattr(mod, "prune_stale_inventory", lambda *_a, **_k: {"torrent_files": 0, "disk_hashes": 0, "skipped": 0})
    monkeypatch.setattr(mod, "_add_log", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_get_checkpoint", lambda _db: "zzz")
    monkeypatch.setattr(mod, "_set_checkpoint", lambda _db, value: calls["checkpoint"].append(value))
    monkeypatch.setattr(mod, "_touch_job", lambda *_a, **_k: None)

    asyncio.run(mod.run_hash_backfill(db, 1, {}))
    assert calls["checkpoint"][-1] == ""
