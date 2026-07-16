from app.jobs.ongoing import _extract_releases_from_schedule
from app.services.release_checkpoint import ReleaseRef


def test_extract_releases_from_schedule_nested_release() -> None:
    payload = [
        {"release": {"id": 100, "alias": "one", "updated_at": "2024-01-01T00:00:00+00:00"}},
        {"release": {"id": 200, "alias": "two", "fresh_at": "2024-02-01T00:00:00+00:00"}},
    ]
    assert _extract_releases_from_schedule(payload) == [
        ReleaseRef(100, "one", "2024-01-01T00:00:00+00:00", None),
        ReleaseRef(200, "two", None, "2024-02-01T00:00:00+00:00"),
    ]


def test_extract_releases_from_schedule_data_wrapper() -> None:
    payload = {"data": [{"release": {"id": 42, "alias": "test"}}]}
    assert _extract_releases_from_schedule(payload) == [ReleaseRef(42, "test", None, None)]


def test_extract_releases_from_schedule_dedup() -> None:
    payload = [
        {"release": {"id": 1, "alias": "a"}},
        {"release": {"id": 1, "alias": "a"}},
    ]
    assert _extract_releases_from_schedule(payload) == [ReleaseRef(1, "a", None, None)]


def test_extract_releases_from_schedule_empty_include_bug_shape() -> None:
    """include=id,alias на верхнем уровне даёт пустые слоты — не должны считаться релизами."""
    payload = [[], [], {"release": {"id": 5, "alias": "ok"}}]
    assert _extract_releases_from_schedule(payload) == [ReleaseRef(5, "ok", None, None)]
