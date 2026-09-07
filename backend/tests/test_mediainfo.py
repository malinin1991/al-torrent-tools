from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from app.db.models import FileMediaInfo, TorrentFile
from app.services.matroska_attachments import (
    build_minimal_mkv_with_attachments,
    build_mkv_attachments_after_cluster,
    read_matroska_attachments,
    read_matroska_attachments_bytes,
)
from app.services.mediainfo import (
    MKV_ATTACHMENTS_KEY,
    _attachments_columns_from_summary,
    _build_summary_from_data,
    format_bitrate_human,
    format_duration_human,
    get_or_extract_mediainfo,
    is_media_filename,
    resolve_mediainfo_version,
    upsert_file_mediainfo,
)


def test_is_media_filename() -> None:
    assert is_media_filename("episode_01.mkv") is True
    assert is_media_filename("video.mp4") is True
    assert is_media_filename("audio.flac") is True
    assert is_media_filename("info.txt") is False
    assert is_media_filename("poster.jpg") is False
    assert is_media_filename(".DS_Store") is False


def test_resolve_mediainfo_version_prefers_library(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.mediainfo._mediainfo_library_version",
        lambda: "libmediainfo 26.03",
    )
    monkeypatch.setattr(
        "app.services.mediainfo._mediainfo_cli_version",
        lambda: "should-not-call",
    )
    assert resolve_mediainfo_version() == "libmediainfo 26.03"


def test_resolve_mediainfo_version_cli_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.mediainfo._mediainfo_library_version",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.mediainfo._mediainfo_cli_version",
        lambda: "MediaInfoLib - v26.03",
    )
    assert resolve_mediainfo_version() == "MediaInfoLib - v26.03"


def test_mediainfo_library_version_from_get_library(monkeypatch) -> None:
    """Версия с /info — то, что вернул _get_library (в т.ч. bundled wheel)."""
    from app.services.mediainfo import _mediainfo_library_version

    class _FakeMediaInfo:
        @staticmethod
        def can_parse() -> bool:
            return True

        @staticmethod
        def _get_library():
            return (object(), 0, "26.03", (26, 3))

    import sys
    import types

    fake_mod = types.ModuleType("pymediainfo")
    fake_mod.MediaInfo = _FakeMediaInfo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymediainfo", fake_mod)
    assert _mediainfo_library_version() == "libmediainfo 26.03"


def test_mediainfo_cli_version_missing(monkeypatch) -> None:
    from app.services.mediainfo import _mediainfo_cli_version

    def _boom(*_a, **_k):
        raise FileNotFoundError("mediainfo")

    monkeypatch.setattr("app.services.mediainfo.subprocess.run", _boom)
    assert _mediainfo_cli_version() == "не установлен"


def test_mediainfo_cli_version_parses_lib_line(monkeypatch) -> None:
    from app.services.mediainfo import _mediainfo_cli_version

    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = "MediaInfo Command line - v24.06\nMediaInfoLib - v24.06\n"
    completed.stderr = ""
    monkeypatch.setattr(
        "app.services.mediainfo.subprocess.run",
        lambda *_a, **_k: completed,
    )
    assert _mediainfo_cli_version() == "MediaInfoLib - v24.06"


def test_format_duration_human() -> None:
    assert format_duration_human(None) == ""
    assert format_duration_human(0) == ""
    assert format_duration_human(45) == "45 с"
    assert format_duration_human(125) == "2 мин 5 с"
    assert format_duration_human(3665) == "1 ч 1 мин 5 с"


def test_format_bitrate_human() -> None:
    assert format_bitrate_human(None) == ""
    assert format_bitrate_human(0) == ""
    assert format_bitrate_human(500) == "500 бит/с"
    assert format_bitrate_human(128_000) == "128 кбит/с"
    assert format_bitrate_human(5_400_000) == "5.4 Мбит/с"
    # MediaInfo иногда отдаёт числовые поля строками
    assert format_bitrate_human("208228") == "208 кбит/с"
    assert format_bitrate_human("not-a-number") == ""


def test_build_summary_from_data() -> None:
    raw_data = {
        "tracks": [
            {
                "track_type": "General",
                "format": "Matroska",
                "duration": 1440000,
                "overall_bit_rate": 2500000,
                "file_size": 450000000,
            },
            {
                "track_type": "Video",
                "format": "HEVC",
                "format_profile": "Main 10@L4@Main",
                "width": 1920,
                "height": 1080,
                "frame_rate": 23.976,
                "bit_rate": 2200000,
                "bit_depth": 10,
                "hdr_format": "SMPTE ST 2086",
            },
            {
                "track_type": "Audio",
                "language": "rus",
                "format": "AAC",
                "channels": 2,
                "bit_rate": 192000,
                "sampling_rate": 48000,
                "title": "AniLibria",
            },
            {
                "track_type": "Text",
                "language": "rus",
                "format": "ASS",
                "title": "Надписи",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert summary["format"] == "Matroska"
    assert summary["duration_sec"] == 1440.0
    assert "24 мин" in summary["duration_human"]
    assert "2.5 Мбит/с" in summary["overall_bit_rate"]
    assert "МиБ" in summary["file_size"]

    assert len(summary["videos"]) == 1
    v = summary["videos"][0]
    assert v["format"] == "HEVC"
    assert v["resolution"] == "1920x1080"
    assert v["bit_depth"] == 10
    assert v["hdr_format"] == "SMPTE ST 2086"

    assert len(summary["audios"]) == 1
    a = summary["audios"][0]
    assert a["language"] == "rus"
    assert a["format"] == "AAC"
    assert a["title"] == "AniLibria"
    assert a["channels"] == "2"
    assert a["sampling_rate"] == "48 кГц"

    assert len(summary["subtitles"]) == 1
    s = summary["subtitles"][0]
    assert s["language"] == "rus"
    assert s["format"] == "ASS"
    assert "encoding" not in s
    assert s["stream_size"] == ""
    assert s["default"] is None
    assert s["forced"] is None
    assert s["original"] is None
    assert summary["fonts"] == []
    assert summary["fonts_total_size"] == ""
    assert summary["fonts_total_bytes"] is None


def test_build_summary_flags_and_original_from_service_kind() -> None:
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Video",
                "format": "AVC",
                "width": 1280,
                "height": 720,
                "default": "Yes",
                "forced": "No",
                "title": "Original",  # название дорожки, не FlagOriginal
            },
            {
                "track_type": "Audio",
                "language": "jpn",
                "format": "AAC",
                "default": "Yes",
                "forced": "No",
                "service_kind": "Original",
                "title": "Original",
            },
            {
                "track_type": "Audio",
                "language": "rus",
                "format": "AAC",
                "default": "No",
                "forced": "No",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    v = summary["videos"][0]
    assert v["default"] is True
    assert v["forced"] is False
    assert v["original"] is False  # title != FlagOriginal; нет service_kind
    assert v["title"] == "Original"

    a0, a1 = summary["audios"]
    assert a0["original"] is True
    assert a0["default"] is True
    assert a1["original"] is False
    assert a1["default"] is False


def test_build_summary_flag_original_matroska_service_kind_o() -> None:
    """Matroska FlagOriginal → ServiceKind «O» (+ String «Original»)."""
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Audio",
                "language": "jpn",
                "format": "AAC",
                "default": "Yes",
                "forced": "No",
                "service_kind": "O",
                "service_kind_string": "Original",
                "title": "Japanese",
            },
            {
                "track_type": "Audio",
                "language": "rus",
                "format": "AAC",
                "default": "No",
                "forced": "No",
                "service_kind": "O",  # только код, без string
            },
            {
                "track_type": "Text",
                "language": "jpn",
                "format": "ASS",
                "default": "No",
                "forced": "No",
                # только string — тоже FlagOriginal
                "service_kind_string": "Original",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert summary["audios"][0]["original"] is True
    assert summary["audios"][1]["original"] is True
    assert summary["subtitles"][0]["original"] is True


def test_build_summary_flag_original_explicit_yes_no_fields() -> None:
    """Явные Yes/No-поля original / flag_original; title не считается флагом."""
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Audio",
                "language": "jpn",
                "format": "AAC",
                "default": "Yes",
                "forced": "No",
                "original": "Yes",
                "title": "Dub",
            },
            {
                "track_type": "Audio",
                "language": "eng",
                "format": "AAC",
                "default": "No",
                "forced": "No",
                "flag_original": "No",
                "title": "Original",
            },
            {
                "track_type": "Text",
                "language": "und",
                "format": "ASS",
                "default": "Yes",
                "forced": "No",
                # Original/Track name — не Yes/No → игнор, fallback false
                "original_track": "Signs & Songs",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert summary["audios"][0]["original"] is True
    assert summary["audios"][1]["original"] is False
    assert summary["audios"][1]["title"] == "Original"
    assert summary["subtitles"][0]["original"] is False


def test_build_summary_subtitle_utf8_plain_text_and_ass() -> None:
    """S_TEXT/UTF8: MediaInfo кладёт кодировку в format — нормализуем в Plain Text."""
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Text",
                "language": "rus",
                "format": "ASS",
                "codec_id": "S_TEXT/ASS",
                "title": "Signs",
                "default": "Yes",
                "forced": "No",
                "stream_size": 102400,
            },
            {
                "track_type": "Text",
                "language": "jpn",
                "format": "UTF-8",
                "codec_id": "S_TEXT/UTF8",
                "codec_id_info": "UTF-8 Plain Text",
                "title": "Full",
                "default": "No",
                "forced": "Yes",
                "stream_size": 512000,
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    ass, plain = summary["subtitles"]
    assert ass["format"] == "ASS"
    assert "encoding" not in ass
    assert "КиБ" in ass["stream_size"] or "МиБ" in ass["stream_size"] or ass["stream_size"]
    assert ass["default"] is True
    assert ass["forced"] is False
    assert ass["original"] is False

    assert plain["format"] == "Plain Text"
    assert "encoding" not in plain
    assert plain["default"] is False
    assert plain["forced"] is True
    assert "МиБ" in plain["stream_size"] or "КиБ" in plain["stream_size"]


def test_build_summary_fonts_from_attachment_tracks() -> None:
    raw_data = {
        "tracks": [
            {
                "track_type": "General",
                "format": "Matroska",
                "attachments": "Arial.ttf / cover.jpg",
            },
            {
                "track_type": "Other",
                "type": "Attachment",
                "title": "Roboto-Regular.ttf",
                "internet_media_type": "application/x-truetype-font",
                "stream_size": 204800,
            },
            {
                "track_type": "Image",
                "type": "Cover",
                "title": "cover.jpg",
                "stream_size": 50000,
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert len(summary["fonts"]) == 1
    font = summary["fonts"][0]
    assert font["name"] == "Roboto-Regular.ttf"
    assert font["size_bytes"] == 204800
    assert "КиБ" in font["size"] or "МиБ" in font["size"]
    assert "truetype" in font["mime"]
    assert summary["fonts_total_bytes"] == 204800
    assert summary["fonts_total_size"]


def test_build_summary_fonts_mime_from_internet_media_type() -> None:
    """Стандартный ключ InternetMediaType → internet_media_type в summary.mime."""
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Other",
                "type": "Attachment",
                "title": "NotoSans.otf",
                "internet_media_type": "font/otf",
                "stream_size": 102400,
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert len(summary["fonts"]) == 1
    assert summary["fonts"][0]["mime"] == "font/otf"
    assert summary["fonts"][0]["size_bytes"] == 102400


def test_build_summary_fonts_mime_from_mime_key() -> None:
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Other",
                "muxing_mode": "Attachment",
                "title": "Legacy.ttf",
                "mime": "application/x-font-ttf",
                "stream_size": 4096,
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert summary["fonts"][0]["mime"] == "application/x-font-ttf"


def test_build_summary_fonts_from_general_attachments_no_extension_mime() -> None:
    """Без EBML-метаданных MIME не угадываем по расширению — пустая строка (UI: —)."""
    raw_data = {
        "tracks": [
            {
                "track_type": "General",
                "format": "Matroska",
                "attachments": "NotoSans.ttf / OpenSans.otf",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert len(summary["fonts"]) == 2
    by_name = {f["name"]: f for f in summary["fonts"]}
    assert set(by_name) == {"NotoSans.ttf", "OpenSans.otf"}
    assert all(f["size_bytes"] is None for f in summary["fonts"])
    assert summary["fonts_total_bytes"] is None
    assert by_name["NotoSans.ttf"]["mime"] == ""
    assert by_name["OpenSans.otf"]["mime"] == ""


def test_read_matroska_attachments_from_minimal_ebml(tmp_path: Path) -> None:
    blob = build_minimal_mkv_with_attachments(
        [
            ("trebucbd_0.ttf", "application/x-truetype-font", b"font-bytes-here"),
            ("cover.jpg", "image/jpeg", b"\xff\xd8\xff"),
            ("AdobeArabic-Bold.otf", "application/vnd.ms-opentype", b"OTTO" + b"\0" * 20),
        ]
    )
    parsed = read_matroska_attachments_bytes(blob)
    assert parsed == [
        {
            "name": "trebucbd_0.ttf",
            "mime": "application/x-truetype-font",
            "size_bytes": 15,
        },
        {"name": "cover.jpg", "mime": "image/jpeg", "size_bytes": 3},
        {
            "name": "AdobeArabic-Bold.otf",
            "mime": "application/vnd.ms-opentype",
            "size_bytes": 24,
        },
    ]

    mkv_path = tmp_path / "sample.mkv"
    mkv_path.write_bytes(blob)
    assert read_matroska_attachments(mkv_path) == parsed


def test_read_matroska_attachments_after_cluster_no_seekhead() -> None:
    """Attachments после Cluster без SeekHead — линейный scan Segment (seek по size)."""
    blob = build_mkv_attachments_after_cluster(
        [("NotoSans.ttf", "font/ttf", b"FONTDATA"), ("cover.png", "image/png", b"PNG")],
        cluster_payload=b"\x01" * 128,
    )
    parsed = read_matroska_attachments_bytes(blob)
    assert parsed == [
        {"name": "NotoSans.ttf", "mime": "font/ttf", "size_bytes": 8},
        {"name": "cover.png", "mime": "image/png", "size_bytes": 3},
    ]


def test_read_matroska_attachments_skips_oversized_string_elements() -> None:
    """FileName с заявленным size > 64KiB — attachment пропускается, без OOM."""
    from app.services.matroska_attachments import (
        _ID_ATTACHED_FILE,
        _ID_ATTACHMENTS,
        _ID_EBML,
        _ID_FILE_DATA,
        _ID_FILE_MIME_TYPE,
        _ID_FILE_NAME,
        _ID_SEGMENT,
        _encode_id,
        _encode_size,
    )

    def elem(eid: int, payload: bytes) -> bytes:
        return _encode_id(eid) + _encode_size(len(payload)) + payload

    def elem_sized(eid: int, size: int, payload: bytes) -> bytes:
        assert len(payload) == size
        return _encode_id(eid) + _encode_size(size) + payload

    ok = elem(
        _ID_ATTACHED_FILE,
        b"".join(
            [
                elem(_ID_FILE_NAME, b"ok.ttf"),
                elem(_ID_FILE_MIME_TYPE, b"font/ttf"),
                elem(_ID_FILE_DATA, b"OK"),
            ]
        ),
    )
    huge = 200_000
    bad = elem(
        _ID_ATTACHED_FILE,
        b"".join(
            [
                elem_sized(_ID_FILE_NAME, huge, b"x" * huge),
                elem(_ID_FILE_MIME_TYPE, b"font/ttf"),
                elem(_ID_FILE_DATA, b"BAD"),
            ]
        ),
    )
    blob = elem(_ID_EBML, elem(0x4282, b"matroska")) + elem(
        _ID_SEGMENT, elem(_ID_ATTACHMENTS, ok + bad)
    )
    parsed = read_matroska_attachments_bytes(blob)
    assert parsed == [{"name": "ok.ttf", "mime": "font/ttf", "size_bytes": 2}]


def test_build_summary_flag_original_ignores_non_original_token() -> None:
    """Подстрока «original» в токене (non-original) не даёт FlagOriginal."""
    raw_data = {
        "tracks": [
            {"track_type": "General", "format": "Matroska"},
            {
                "track_type": "Audio",
                "language": "eng",
                "format": "AAC",
                "default": "Yes",
                "forced": "No",
                "service_kind_string": "non-original",
            },
            {
                "track_type": "Audio",
                "language": "jpn",
                "format": "AAC",
                "default": "No",
                "forced": "No",
                "service_kind": "O",
            },
        ]
    }
    summary = _build_summary_from_data(raw_data)
    assert summary["audios"][0]["original"] is False
    assert summary["audios"][1]["original"] is True


def test_build_summary_fonts_mime_from_mkv_attachments() -> None:
    raw_data = {
        "tracks": [
            {
                "track_type": "General",
                "format": "Matroska",
                "attachments": "trebucbd_0.ttf / cover.jpg",
            },
        ],
        MKV_ATTACHMENTS_KEY: [
            {
                "name": "trebucbd_0.ttf",
                "mime": "application/x-truetype-font",
                "size_bytes": 135006,
            },
            {
                "name": "cover.jpg",
                "mime": "image/jpeg",
                "size_bytes": 50000,
            },
        ],
    }
    summary = _build_summary_from_data(raw_data)
    assert len(summary["fonts"]) == 1
    font = summary["fonts"][0]
    assert font["name"] == "trebucbd_0.ttf"
    assert font["mime"] == "application/x-truetype-font"
    assert font["size_bytes"] == 135006
    assert summary["fonts_total_bytes"] == 135006


def test_attachments_columns_from_summary() -> None:
    rows, count, total = _attachments_columns_from_summary(
        {
            "fonts": [
                {
                    "name": "a.ttf",
                    "mime": "application/x-truetype-font",
                    "size_bytes": 100,
                    "size": "100 Б",
                },
                {"name": "b.otf", "mime": "application/vnd.ms-opentype", "size_bytes": 50},
            ]
        }
    )
    assert count == 2
    assert total == 150
    assert rows == [
        {"name": "a.ttf", "mime": "application/x-truetype-font", "size_bytes": 100},
        {"name": "b.otf", "mime": "application/vnd.ms-opentype", "size_bytes": 50},
    ]
    assert _attachments_columns_from_summary({}) == ([], 0, None)
    assert _attachments_columns_from_summary({"fonts": [{"name": "x.ttf", "mime": ""}]}) == (
        [{"name": "x.ttf", "mime": "", "size_bytes": None}],
        1,
        None,
    )


def test_get_or_extract_mediainfo_not_found() -> None:
    db = MagicMock()
    db.get.return_value = None
    res = get_or_extract_mediainfo(db, 999)
    assert res["ok"] is False
    assert res["status"] == "not_found"


def test_get_or_extract_mediainfo_checking() -> None:
    db = MagicMock()
    tf = TorrentFile(
        id=1,
        full_path="/media/test.mkv",
        relative_path="test.mkv",
        is_checking=True,
    )
    db.get.return_value = tf
    res = get_or_extract_mediainfo(db, 1)
    assert res["ok"] is False
    assert res["status"] == "in_progress"


def test_get_or_extract_mediainfo_cached() -> None:
    db = MagicMock()
    tf = TorrentFile(
        id=1,
        full_path="/media/test.mkv",
        relative_path="test.mkv",
        is_checking=False,
    )
    fmi = FileMediaInfo(
        full_path=str(Path("/media/test.mkv").resolve()),
        file_size=1000,
        mtime=12345.0,
        summary_json={"format": "Matroska"},
        raw_text="MediaInfo report text",
    )
    db.get.return_value = tf
    db.scalar.return_value = fmi

    with patch("pathlib.Path.is_file", return_value=False):
        res = get_or_extract_mediainfo(db, 1)
        assert res["ok"] is True
        assert res["status"] == "ready"
        assert res["summary"] == {"format": "Matroska"}
        assert res["raw_text"] == "MediaInfo report text"


def test_get_or_extract_mediainfo_rebuilds_stale_summary_from_raw_json() -> None:
    """Старый summary без fonts/default — пересобираем из raw_json без файла на диске."""
    db = MagicMock()
    tf = TorrentFile(
        id=1,
        full_path="/media/test.mkv",
        relative_path="test.mkv",
        is_checking=False,
    )
    raw = {
        "tracks": [
            {
                "track_type": "General",
                "format": "Matroska",
                "attachments": "NotoSans.ttf",
            },
            {
                "track_type": "Text",
                "language": "rus",
                "format": "UTF-8",
                "codec_id": "S_TEXT/UTF8",
                "codec_id_info": "UTF-8 Plain Text",
                "default": "Yes",
                "forced": "No",
                "service_kind": "O",
            },
        ]
    }
    fmi = FileMediaInfo(
        full_path=str(Path("/media/test.mkv").resolve()),
        file_size=1000,
        mtime=12345.0,
        summary_json={"format": "Matroska", "videos": [], "audios": [], "subtitles": []},
        raw_json=raw,
        raw_text="old report",
    )
    db.get.return_value = tf
    db.scalar.return_value = fmi

    with patch("pathlib.Path.is_file", return_value=False):
        res = get_or_extract_mediainfo(db, 1)
    assert res["ok"] is True
    assert res["cached_only"] is True
    summary = res["summary"]
    assert "fonts" in summary
    assert len(summary["fonts"]) == 1
    assert summary["fonts"][0]["name"] == "NotoSans.ttf"
    assert summary["subtitles"][0]["format"] == "Plain Text"
    assert "encoding" not in summary["subtitles"][0]
    assert summary["subtitles"][0]["default"] is True
    assert summary["subtitles"][0]["original"] is True


def test_upsert_file_mediainfo_gate_skip(tmp_path: Path) -> None:
    sample_file = tmp_path / "ep1.mkv"
    sample_file.write_bytes(b"sample media content for test")
    st = sample_file.stat()

    db = MagicMock()
    existing = FileMediaInfo(
        full_path=str(sample_file.resolve()),
        file_size=st.st_size,
        mtime=float(st.st_mtime),
        summary_json={"test": 1},
    )
    db.scalar.return_value = existing

    with patch("app.services.mediainfo.parse_media_file") as mock_parse:
        res = upsert_file_mediainfo(db, str(sample_file), force=False)
        assert res is existing
        mock_parse.assert_not_called()


def test_upsert_file_mediainfo_writes_attachments_columns(tmp_path: Path) -> None:
    sample_file = tmp_path / "ep1.mkv"
    sample_file.write_bytes(b"sample media content for test")

    db = MagicMock()
    db.scalar.return_value = None

    summary = {
        "format": "Matroska",
        "fonts": [
            {
                "name": "arial.ttf",
                "mime": "application/x-truetype-font",
                "size_bytes": 1000,
                "size": "1000 Б",
            },
            {"name": "empty.otf", "mime": "", "size_bytes": None, "size": ""},
        ],
        "fonts_total_bytes": 1000,
    }
    raw_json = {"tracks": [], MKV_ATTACHMENTS_KEY: []}
    raw_text = "report"

    with patch(
        "app.services.mediainfo.parse_media_file",
        return_value=(summary, raw_json, raw_text),
    ):
        res = upsert_file_mediainfo(db, str(sample_file), force=True)

    assert res is not None
    db.add.assert_called_once()
    created = db.add.call_args[0][0]
    assert isinstance(created, FileMediaInfo)
    assert created.attachments_count == 2
    assert created.attachments_total_bytes == 1000
    assert created.attachments_json == [
        {"name": "arial.ttf", "mime": "application/x-truetype-font", "size_bytes": 1000},
        {"name": "empty.otf", "mime": "", "size_bytes": None},
    ]
    assert created.summary_json == summary
    db.commit.assert_called()
    db.refresh.assert_called()


def test_upsert_file_mediainfo_updates_attachments_on_existing(tmp_path: Path) -> None:
    sample_file = tmp_path / "ep1.mkv"
    sample_file.write_bytes(b"sample media content for test")
    st = sample_file.stat()

    db = MagicMock()
    existing = FileMediaInfo(
        full_path=str(sample_file.resolve()),
        file_size=st.st_size,
        mtime=float(st.st_mtime),
        summary_json={},
        attachments_json=[],
        attachments_count=0,
        attachments_total_bytes=None,
    )
    db.scalar.return_value = existing

    summary = {
        "fonts": [
            {"name": "x.ttf", "mime": "application/x-truetype-font", "size_bytes": 42},
        ]
    }
    with patch(
        "app.services.mediainfo.parse_media_file",
        return_value=(summary, {}, "text"),
    ):
        res = upsert_file_mediainfo(db, str(sample_file), force=True)

    assert res is existing
    assert existing.attachments_count == 1
    assert existing.attachments_total_bytes == 42
    assert existing.attachments_json == [
        {"name": "x.ttf", "mime": "application/x-truetype-font", "size_bytes": 42}
    ]
    db.add.assert_not_called()
    db.commit.assert_called()


def test_run_mediainfo_sync() -> None:
    import asyncio
    from app.jobs.mediainfo_sync import run_mediainfo_sync

    db = MagicMock()
    db.scalars.return_value.all.return_value = ["/media/show/ep01.mkv"]
    db.execute.return_value.first.return_value = None

    st = MagicMock()
    st.st_size = 1024
    st.st_mtime = 200.0

    with patch("app.jobs.mediainfo_sync.is_stop_requested", return_value=False), \
         patch("app.jobs.mediainfo_sync.is_media_filename", return_value=True), \
         patch.object(Path, "is_file", return_value=True), \
         patch.object(Path, "stat", return_value=st), \
         patch("app.jobs.mediainfo_sync.upsert_file_mediainfo", return_value=MagicMock()):
        asyncio.run(run_mediainfo_sync(db, 10, {"mode": "incremental"}))
        assert db.add.called
        assert db.commit.called


def test_mediainfo_api_get_and_refresh() -> None:
    from app.api import rest

    db = MagicMock()
    with patch("app.services.mediainfo.get_or_extract_mediainfo") as mock_get:
        mock_get.return_value = {"ok": True, "status": "ready", "summary": {"format": "MKV"}}
        res = rest.get_torrent_file_mediainfo(42, db=db)
        assert res["ok"] is True
        mock_get.assert_called_with(db, 42, force=False)

        res_refresh = rest.refresh_torrent_file_mediainfo(42, db=db)
        assert res_refresh["ok"] is True
        mock_get.assert_called_with(db, 42, force=True)


def test_mediainfo_job_action_run() -> None:
    import asyncio
    from app.main import run_job_action

    db = MagicMock()
    request = MagicMock()

    with patch("app.main.job_runner.create_job") as mock_create, \
         patch("app.main.job_runner.schedule_job") as mock_sched, \
         patch("app.main.templates.TemplateResponse") as mock_tpl:
        job = MagicMock()
        job.id = 55
        job.type = "mediainfo_sync"
        mock_create.return_value = job

        asyncio.run(
            run_job_action(
                request,
                job_type="mediainfo_sync",
                full_scan=True,
                db=db,
            )
        )
        mock_create.assert_called_once_with(
            db, "mediainfo_sync", {"mode": "full", "force": True, "workers": 4}
        )
        mock_sched.assert_called_once_with(55)

