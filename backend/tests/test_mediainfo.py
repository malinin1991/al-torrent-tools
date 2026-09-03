from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from app.db.models import FileMediaInfo, TorrentFile
from app.services.mediainfo import (
    _build_summary_from_data,
    format_bitrate_human,
    format_duration_human,
    get_or_extract_mediainfo,
    is_media_filename,
    upsert_file_mediainfo,
)


def test_is_media_filename() -> None:
    assert is_media_filename("episode_01.mkv") is True
    assert is_media_filename("video.mp4") is True
    assert is_media_filename("audio.flac") is True
    assert is_media_filename("info.txt") is False
    assert is_media_filename("poster.jpg") is False
    assert is_media_filename(".DS_Store") is False


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

    assert len(summary["subtitles"]) == 1
    s = summary["subtitles"][0]
    assert s["language"] == "rus"
    assert s["format"] == "ASS"


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

