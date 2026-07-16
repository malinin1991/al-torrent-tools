from app.jobs.ongoing import _extract_releases_from_schedule


def test_extract_releases_from_schedule_nested_release() -> None:
    payload = [
        {"release": {"id": 100, "alias": "one"}},
        {"release": {"id": 200, "alias": "two"}},
    ]
    assert _extract_releases_from_schedule(payload) == [(100, "one"), (200, "two")]


def test_extract_releases_from_schedule_data_wrapper() -> None:
    payload = {"data": [{"release": {"id": 42, "alias": "test"}}]}
    assert _extract_releases_from_schedule(payload) == [(42, "test")]


def test_extract_releases_from_schedule_dedup() -> None:
    payload = [
        {"release": {"id": 1, "alias": "a"}},
        {"release": {"id": 1, "alias": "a"}},
    ]
    assert _extract_releases_from_schedule(payload) == [(1, "a")]


def test_extract_releases_from_schedule_empty_include_bug_shape() -> None:
    """include=id,alias на верхнем уровне даёт пустые слоты — не должны считаться релизами."""
    payload = [[], [], {"release": {"id": 5, "alias": "ok"}}]
    assert _extract_releases_from_schedule(payload) == [(5, "ok")]
