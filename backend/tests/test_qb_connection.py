from unittest.mock import MagicMock

import pytest

from app.services import qbittorrent as qb_mod


def test_qb_connection_requires_host() -> None:
    with pytest.raises(RuntimeError, match="хост"):
        qb_mod.test_qb_connection(host="", port=8080, username="a", password="b")


def test_qb_connection_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.app_version.return_value = "v4.6.0"
    client.app_web_api_version.return_value = "2.9.3"
    client.torrents_info.return_value = [1, 2, 3]
    monkeypatch.setattr(qb_mod.qbittorrentapi, "Client", MagicMock(return_value=client))

    result = qb_mod.test_qb_connection(host="qb.local", port=8080, username="admin", password="secret")

    assert result["ok"] is True
    assert result["version"] == "v4.6.0"
    assert result["torrents"] == 3
    client.auth_log_in.assert_called_once()
    client.auth_log_out.assert_called_once()
