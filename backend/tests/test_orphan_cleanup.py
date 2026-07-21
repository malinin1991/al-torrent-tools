from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.jobs.orphan_cleanup import find_orphan_files, run_orphan_cleanup
from app.services.qb_inventory import InventoryFile, InventoryResult, prune_stale_inventory
from app.services.torrent_files_meta import collect_known_paths_from_qb


def test_collect_known_paths_from_qb_resolves_files(monkeypatch, tmp_path: Path) -> None:
    media_root = tmp_path / "anilibria"
    show_dir = media_root / "Show"
    ep1 = show_dir / "ep01.mkv"
    ep2 = show_dir / "ep02.mkv"
    orphan = media_root / "orphan.mkv"
    ep1.parent.mkdir(parents=True)
    ep1.write_bytes(b"1")
    ep2.write_bytes(b"2")
    orphan.write_bytes(b"x")

    monkeypatch.setattr("app.services.torrent_files_meta.resolve_media_root", lambda: media_root)
    monkeypatch.setattr("app.services.torrent_files_meta.is_under_media_root", lambda path, **_: True)

    torrent = SimpleNamespace(hash="a" * 40, save_path=str(show_dir))
    qb_files = [
        SimpleNamespace(name="ep01.mkv"),
        SimpleNamespace(name="ep02.mkv"),
    ]
    qb = MagicMock()
    qb.torrents_info.return_value = [torrent]
    qb.torrents_files.return_value = qb_files
    monkeypatch.setattr("qbittorrentapi.Client", lambda **_: qb)

    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        host="qb.local",
        port=8080,
        username="u",
        password_encrypted="p",
    )

    known = collect_known_paths_from_qb(db)

    assert ep1.resolve() in known
    assert ep2.resolve() in known
    assert orphan.resolve() not in known


def test_orphan_cleanup_forces_dry_run_when_delete_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "orphan.mkv"
    target.write_bytes(b"orphan")

    db = MagicMock()
    logs: list[str] = []

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(
            valid_hashes={"a" * 40},
            files=[
                InventoryFile(
                    info_hash="a" * 40,
                    torrent_id=1,
                    release_id=1,
                    relative_path="keep.mkv",
                    size=1,
                    file_index=0,
                    selected=True,
                    full_path=str((tmp_path / "keep.mkv").resolve()),
                    folder_key=str(tmp_path.resolve()),
                )
            ],
        ),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", False)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert target.exists()
    assert any("CLEANUP_ALLOW_DELETE=false" in msg for msg in logs)


def test_orphan_cleanup_aborts_apply_when_inventory_empty(
    monkeypatch, tmp_path: Path
) -> None:
    victim = tmp_path / "all.mkv"
    victim.write_bytes(b"data")
    logs: list[str] = []
    db = MagicMock()

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(valid_hashes=set(), files=[]),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert victim.exists()
    assert any("APPLY ОТМЕНЁН" in msg for msg in logs)
    assert any("сканирование ФС пропущено" in msg for msg in logs)


def test_orphan_cleanup_apply_fails_when_root_not_writable(
    monkeypatch, tmp_path: Path
) -> None:
    logs: list[str] = []
    db = MagicMock()
    keep = tmp_path / "keep.mkv"
    keep.write_bytes(b"1")

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: tmp_path)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(
            valid_hashes={"a" * 40},
            files=[
                InventoryFile(
                    info_hash="a" * 40,
                    torrent_id=1,
                    release_id=1,
                    relative_path="keep.mkv",
                    size=1,
                    file_index=0,
                    selected=True,
                    full_path=str(keep.resolve()),
                    folder_key=str(tmp_path.resolve()),
                )
            ],
        ),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup.find_orphan_files", lambda **_: [tmp_path / "x.mkv"])
    monkeypatch.setattr("app.jobs.orphan_cleanup.media_root_is_writable", lambda _p: False)
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)

    with pytest.raises(RuntimeError, match="недоступен для записи"):
        asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    media = tmp_path / "anilibria"
    media.mkdir()
    keep = media / "keep.mkv"
    orphan = media / "gone.mkv"
    keep.write_bytes(b"1")
    orphan.write_bytes(b"2")
    found = find_orphan_files(media_root=media, known={keep.resolve()})
    assert orphan.resolve() in found
    assert keep.resolve() not in found


def test_prune_skips_when_inventory_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.qb_inventory.resolve_media_root", lambda: tmp_path)
    db = MagicMock()
    result = prune_stale_inventory(db, InventoryResult(valid_hashes=set(), files=[]))
    assert result["skipped"] == 1
    assert result["torrent_files"] == 0
    db.scalars.assert_not_called()
    db.delete.assert_not_called()
