"""Candidate-only default policy regression; no performance measurement."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.jobs import mediainfo_sync as job


@pytest.mark.parametrize("requested,expected", [(None, 4), (1, 1), (2, 2), (4, 4), (8, 8)])
def test_incremental_default_four_preserves_explicit_workers(monkeypatch, requested, expected):
    db = MagicMock()
    db.scalars.return_value.all.return_value = ["/anilibria/episode.mkv"]
    calls = []

    def sync(session, paths, **kwargs):
        calls.append((session, paths, kwargs))
        return dict(total=1, scanned=1, updated=0, skipped=1, missing=0, errors=0, stopped=0)

    monkeypatch.setattr(job, "mediainfo_paths_parallel", sync)
    monkeypatch.setattr(job, "is_stop_requested", lambda *_: False)
    params = {"mode": "incremental"}
    if requested is not None:
        params["workers"] = requested
    asyncio.run(job.run_mediainfo_sync(db, 10, params))
    assert len(calls) == 1
    session, paths, options = calls[0]
    assert session is db and paths == ["/anilibria/episode.mkv"]
    assert options["workers"] == expected and options["force"] is False
    assert options["should_stop"]() is False
