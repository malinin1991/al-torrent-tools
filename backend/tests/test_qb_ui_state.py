from app.services.qbittorrent import map_qb_torrent_ui_state


def test_map_qb_torrent_ui_state() -> None:
    assert map_qb_torrent_ui_state("downloading", 0.4) == "downloading"
    assert map_qb_torrent_ui_state("stalledDL", 0.1) == "downloading"
    assert map_qb_torrent_ui_state("uploading", 1.0) == "seeding"
    assert map_qb_torrent_ui_state("stalledUP", 1.0) == "seeding"
    assert map_qb_torrent_ui_state("pausedUP", 1.0) == "stopped"
    assert map_qb_torrent_ui_state("stoppedDL", 0.5) == "stopped"
    assert map_qb_torrent_ui_state("error", 0.2) == "error"
    assert map_qb_torrent_ui_state("missingFiles", 0.0) == "error"
    assert map_qb_torrent_ui_state("", 1.0) == "seeding"
    assert map_qb_torrent_ui_state("", 0.3) == "downloading"
