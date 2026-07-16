from app.services.torrent_archive import TorrentArchiveService


def test_extract_torrent_type_from_codec_label() -> None:
    payload = {"codec": {"label": "HEVC", "value": "x265/HEVC"}}
    assert TorrentArchiveService.extract_torrent_type(payload) == "HEVC"


def test_extract_torrent_type_combined() -> None:
    payload = {
        "type": {"value": "WEBRip", "description": "WEBRip"},
        "quality": {"value": "1080p", "description": "1080p"},
        "codec": {"label": "HEVC", "value": "x265/HEVC"},
    }
    assert TorrentArchiveService.extract_torrent_type(payload) == "WEBRip 1080p HEVC"


def test_extract_torrent_type_bdrip() -> None:
    payload = {
        "type": {"value": "BDRip"},
        "quality": {"value": "1080p"},
        "codec": {"label": "AVC", "value": "x264/AVC"},
    }
    assert TorrentArchiveService.extract_torrent_type(payload) == "BDRip 1080p AVC"


def test_extract_torrent_description() -> None:
    payload = {"description": "1-189"}
    assert TorrentArchiveService._extract_torrent_description(payload) == "1-189"


def test_build_category_anilibria_year() -> None:
    assert TorrentArchiveService._build_category({"year": 2026, "season": {"value": "summer"}}) == "AniLibria/2026"


def test_build_category_anilibria_without_year() -> None:
    assert TorrentArchiveService._build_category({}) == "AniLibria"
