from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.jobs.orphan_cleanup import (
    find_empty_dirs,
    find_junk_dirs,
    find_junk_files,
    find_orphan_files,
    known_paths_for_orphan_scan,
    run_orphan_cleanup,
)
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


def test_find_orphan_files_skips_known(tmp_path: Path) -> None:
    media = tmp_path / "anilibria"
    media.mkdir()
    keep = media / "keep.mkv"
    orphan = media / "gone.mkv"
    keep.write_bytes(b"1")
    orphan.write_bytes(b"2")
    found = find_orphan_files(media_root=media, known={keep.resolve()})
    assert orphan.resolve() in found
    assert keep.resolve() not in found


def test_find_junk_files_detects_ds_store_and_appledouble(tmp_path: Path) -> None:
    media = tmp_path / "anilibria"
    show = media / "Show"
    show.mkdir(parents=True)
    ds = show / ".DS_Store"
    apple = show / "._ep01.mkv"
    thumbs = show / "Thumbs.db"
    real = show / "ep01.mkv"
    ds.write_bytes(b"x")
    apple.write_bytes(b"x")
    thumbs.write_bytes(b"x")
    real.write_bytes(b"media")

    junk = find_junk_files(media_root=media)
    assert ds.resolve() in junk
    assert apple.resolve() in junk
    assert thumbs.resolve() in junk
    assert real.resolve() not in junk


def test_find_junk_dirs_detects_macosx(tmp_path: Path) -> None:
    media = tmp_path / "anilibria"
    junk_dir = media / "Show" / "__MACOSX"
    junk_dir.mkdir(parents=True)
    (junk_dir / "x").write_bytes(b"1")
    found = find_junk_dirs(media_root=media)
    assert junk_dir.resolve() in found


def test_find_empty_dirs_deepest_first(tmp_path: Path) -> None:
    media = tmp_path / "anilibria"
    empty_leaf = media / "2012" / "EmptyShow"
    empty_leaf.mkdir(parents=True)
    kept = media / "2012" / "Keep"
    kept.mkdir(parents=True)
    (kept / "ep.mkv").write_bytes(b"1")

    found = find_empty_dirs(media_root=media)
    assert empty_leaf.resolve() in found
    assert kept.resolve() not in found
    assert media.resolve() not in found


def test_paths_total_size_and_orphan_summary_includes_size(
    monkeypatch, tmp_path: Path
) -> None:
    from app.jobs.orphan_cleanup import paths_total_size
    from app.services.file_hasher import format_file_size

    media = tmp_path / "anilibria"
    media.mkdir()
    orphan = media / "big.mkv"
    orphan.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB
    junk = media / ".DS_Store"
    junk.write_bytes(b"j" * 100)

    assert paths_total_size([orphan]) == 2 * 1024 * 1024

    logs: list[str] = []
    db = MagicMock()
    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: media)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(valid_hashes={"a" * 40}, files=[]),
    )
    # Пустой known → orphan-медиа не сканируем; подменим find_* напрямую.
    monkeypatch.setattr("app.jobs.orphan_cleanup.find_orphan_files", lambda **_: [orphan])
    monkeypatch.setattr("app.jobs.orphan_cleanup.find_junk_files", lambda **_: [junk])
    monkeypatch.setattr("app.jobs.orphan_cleanup.find_junk_dirs", lambda **_: [])
    monkeypatch.setattr("app.jobs.orphan_cleanup.find_empty_dirs", lambda **_: [])
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.known_paths_for_orphan_scan",
        lambda *_a, **_k: ({media / "known.mkv"}, 0),
    )
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup._add_log",
        lambda _db, _job_id, msg, level="info": logs.append(msg),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": True, "apply": False}))

    expected = format_file_size(2 * 1024 * 1024 + 100)
    assert any(f"размер кандидатов={expected}" in msg for msg in logs)
    assert any("dry-run" in msg and "кандидаты" in msg for msg in logs)


def test_orphan_cleanup_apply_removes_junk_and_empty_dirs(
    monkeypatch, tmp_path: Path
) -> None:
    media = tmp_path / "anilibria"
    show = media / "Show"
    empty = media / "EmptyYear" / "EmptyShow"
    empty.mkdir(parents=True)
    show.mkdir(parents=True)
    keep = show / "ep01.mkv"
    keep.write_bytes(b"ok")
    ds = show / ".DS_Store"
    ds.write_bytes(b"junk")
    orphan_media = media / "orphan.mkv"
    orphan_media.write_bytes(b"x")

    logs: list[str] = []
    db = MagicMock()
    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: media)
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
                    relative_path="Show/ep01.mkv",
                    size=2,
                    file_index=0,
                    selected=True,
                    full_path=str(keep.resolve()),
                    folder_key=str(show.resolve()),
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup._add_log",
        lambda _db, _job_id, msg, level="info": logs.append(msg),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)
    monkeypatch.setattr("app.jobs.orphan_cleanup.media_root_is_writable", lambda _p: True)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert keep.exists()
    assert not orphan_media.exists()
    assert not ds.exists()
    assert not empty.exists()
    assert not (media / "EmptyYear").exists()
    assert any("junk_files=" in msg for msg in logs)
    assert any("empty_dirs=" in msg for msg in logs)


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


def test_orphan_cleanup_skips_media_orphans_when_inventory_empty_but_cleans_junk(
    monkeypatch, tmp_path: Path
) -> None:
    media = tmp_path / "anilibria"
    media.mkdir()
    victim = media / "all.mkv"
    victim.write_bytes(b"data")
    ds = media / ".DS_Store"
    ds.write_bytes(b"j")
    empty = media / "Empty"
    empty.mkdir()

    logs: list[str] = []
    db = MagicMock()

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: media)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(valid_hashes=set(), files=[]),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)
    monkeypatch.setattr("app.jobs.orphan_cleanup.media_root_is_writable", lambda _p: True)

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert victim.exists()  # медиа не трогаем при пустом inventory
    assert not ds.exists()
    assert not empty.exists()
    assert any("orphan-медиа ОТМЕНЁН" in msg for msg in logs)


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
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.media_root_writable_status",
        lambda _p: (False, "probe failed errno=EROFS"),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)

    with pytest.raises(RuntimeError, match="недоступен для записи"):
        asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))


def test_prune_skips_when_inventory_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.qb_inventory.resolve_media_root", lambda: tmp_path)
    db = MagicMock()
    result = prune_stale_inventory(db, InventoryResult(valid_hashes=set(), files=[]))
    assert result["skipped"] == 1
    assert result["torrent_files"] == 0
    db.scalars.assert_not_called()
    db.delete.assert_not_called()


def test_known_paths_protects_failed_hashes_from_db(tmp_path: Path) -> None:
    """Пути failed_hashes из torrent_files входят в known (не orphan)."""
    media = tmp_path / "anilibria"
    media.mkdir()
    valid = media / "valid.mkv"
    failed = media / "failed.mkv"
    orphan = media / "orphan.mkv"
    valid.write_bytes(b"v")
    failed.write_bytes(b"f")
    orphan.write_bytes(b"o")

    failed_hash = "a" * 40
    valid_hash = "b" * 40
    inventory = InventoryResult(
        valid_hashes={valid_hash},
        failed_hashes={failed_hash},
        files=[
            InventoryFile(
                info_hash=valid_hash,
                torrent_id=1,
                release_id=1,
                relative_path="valid.mkv",
                size=1,
                file_index=0,
                selected=True,
                full_path=str(valid.resolve()),
                folder_key=str(media.resolve()),
            )
        ],
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [
        SimpleNamespace(info_hash=failed_hash, full_path=str(failed.resolve())),
    ]

    known, protected = known_paths_for_orphan_scan(db, inventory)
    assert protected == 1
    assert valid.resolve() in known
    assert failed.resolve() in known

    orphans = find_orphan_files(media_root=media, known=known)
    assert orphan.resolve() in orphans
    assert failed.resolve() not in orphans
    assert valid.resolve() not in orphans


def test_orphan_cleanup_skips_media_orphans_when_failed_hashes(
    monkeypatch, tmp_path: Path
) -> None:
    """Любой failed_hashes → orphan-медиа не трогаем (даже без строк в torrent_files)."""
    media = tmp_path / "anilibria"
    media.mkdir()
    keep_valid = media / "keep.mkv"
    keep_failed = media / "no_db_yet.mkv"
    victim = media / "orphan.mkv"
    ds = media / ".DS_Store"
    keep_valid.write_bytes(b"1")
    keep_failed.write_bytes(b"2")
    victim.write_bytes(b"3")
    ds.write_bytes(b"j")

    failed_hash = "a" * 40
    valid_hash = "b" * 40
    logs: list[str] = []
    db = MagicMock()
    db.scalars.return_value.all.return_value = []

    monkeypatch.setattr("app.jobs.orphan_cleanup.resolve_media_root", lambda: media)
    monkeypatch.setattr("app.jobs.orphan_cleanup.connect_master", lambda _db: MagicMock())
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.build_inventory",
        lambda *_a, **_k: InventoryResult(
            valid_hashes={valid_hash},
            failed_hashes={failed_hash},
            files=[
                InventoryFile(
                    info_hash=valid_hash,
                    torrent_id=1,
                    release_id=1,
                    relative_path="keep.mkv",
                    size=1,
                    file_index=0,
                    selected=True,
                    full_path=str(keep_valid.resolve()),
                    folder_key=str(media.resolve()),
                )
            ],
        ),
    )
    monkeypatch.setattr("app.jobs.orphan_cleanup._add_log", lambda _db, _job_id, msg, level="info": logs.append(msg))
    monkeypatch.setattr("app.jobs.orphan_cleanup.settings.cleanup_allow_delete", True)
    monkeypatch.setattr(
        "app.jobs.orphan_cleanup.media_root_writable_status",
        lambda _p: (True, "ok"),
    )

    asyncio.run(run_orphan_cleanup(db, 1, {"dry_run": False, "apply": True}))

    assert keep_valid.exists()
    assert keep_failed.exists()
    assert victim.exists()  # media orphans skipped
    assert not ds.exists()  # junk still cleaned
    assert any("orphan-медиа ПРОПУЩЕН" in msg for msg in logs)