from app.services.releases_view import format_bytes


def test_format_bytes() -> None:
    assert format_bytes(None) == "-"
    assert format_bytes(500) == "500 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
