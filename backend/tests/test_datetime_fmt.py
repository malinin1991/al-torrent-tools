from datetime import datetime

from app.utils.datetime_fmt import as_utc_iso


def test_as_utc_iso_naive_datetime() -> None:
    assert as_utc_iso(datetime(2026, 7, 16, 10, 43, 10, 689657)) == "2026-07-16T10:43:10.689657Z"


def test_as_utc_iso_string() -> None:
    assert as_utc_iso("2026-07-16 10:43:10.689657") == "2026-07-16T10:43:10.689657Z"


def test_as_utc_iso_none() -> None:
    assert as_utc_iso(None) == ""
