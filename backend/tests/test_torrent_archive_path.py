from pathlib import Path

from app.core.config import default_torrent_storage_dir, settings
from app.services.torrent_archive import resolve_torrent_storage_root


def test_resolve_torrent_storage_root_uses_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TORRENT_STORAGE_DIR", str(tmp_path / "custom"))
    assert resolve_torrent_storage_root() == tmp_path / "custom"


def test_resolve_torrent_storage_root_falls_back_to_settings(monkeypatch) -> None:
    monkeypatch.delenv("TORRENT_STORAGE_DIR", raising=False)
    root = resolve_torrent_storage_root()
    assert root == Path(settings.torrent_storage_dir)
    assert root.name == "torrents"
    assert root.parent.name == "data"
    # Не должен указывать на корень ФС (/data/torrents)
    assert root != Path("/data/torrents")
    assert str(root) != "/data/torrents"
    assert "data" in root.parts


def test_default_torrent_storage_dir_is_safe() -> None:
    root = Path(default_torrent_storage_dir())
    assert root.name == "torrents"
    assert root.parent.name == "data"
    assert root != Path("/data/torrents")
    # Локально — под репозиторием; в Docker — под /app
    assert root.is_absolute()
