"""Свадьба светлячков: история WEBRip не старит SLA нового WEB-DL.

Snapshot релиза hotaru-no-yomeiri (10317) на 24.10.2026. API timestamps,
ID и текущие hashes фиксированы по истории прода; для старых одноэпизодных
версий несущественные local timestamps заданы позже API-clock.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    content_file_keys,
    find_unpaired_avc,
    release_ids_matching_hevc_filter,
    sync_hevc_pair_events_for_release,
)


_RELEASE_ID = 10317
_AVC_UPLOAD = datetime(2026, 10, 24, 12, 22, 13)
_FROZEN_NOW = datetime(2026, 10, 24, 14, 25)
_SLA_BOUNDARY = _AVC_UPLOAD + timedelta(hours=HEVC_SLA_HOURS)
_AVC_HASH = "080e5238e7372ad6a87e9377b63fdb0a787d757f"
_HEVC_HASH = "1438ecf3a883dcb9611bb538b7ee4b478985a5ea"
_PRIOR_AVC_HASH = "5b311b507896c9fa88e734d87bac6df7950c8a08"


def _archive(
    archive_id: int,
    torrent_id: int,
    codec: str,
    episodes: str,
    api_created_at: datetime,
    *,
    rip_type: str = "WEB-DL",
    active: bool = False,
    created_at: datetime | None = None,
    info_hash: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=archive_id,
        release_id=_RELEASE_ID,
        torrent_id=torrent_id,
        torrent_description=episodes,
        torrent_type=f"{rip_type} 1080p {codec}",
        quality_json={
            "type": {"value": rip_type},
            "quality": {"value": "1080p"},
            "codec": {"label": codec, "value": f"x/{codec}"},
        },
        api_created_at=api_created_at,
        created_at=created_at or api_created_at + timedelta(minutes=1),
        api_present=active,
        superseded=not active,
        info_hash=info_hash or f"{archive_id:040x}",
        ignore_hevc=False,
    )


@pytest.fixture
def hotaru_snapshot() -> tuple[list[SimpleNamespace], dict[int, frozenset[str]]]:
    rows = [
        _archive(
            4896, 40514, "AVC", "1-3", _AVC_UPLOAD,
            active=True,
            created_at=datetime(2026, 10, 24, 13, 11, 51, 291695),
            info_hash=_AVC_HASH,
        ),
        _archive(
            4812, 40428, "HEVC", "1-2", datetime(2026, 10, 17, 18, 15, 5),
            active=True,
            created_at=datetime(2026, 10, 17, 18, 15, 29, 447394),
            info_hash=_HEVC_HASH,
        ),
        _archive(
            4808, 40424, "AVC", "1-2", datetime(2026, 10, 17, 16, 29, 37),
            created_at=datetime(2026, 10, 17, 16, 31, 29, 262214),
            info_hash=_PRIOR_AVC_HASH,
        ),
    ]
    for archive_id, torrent_id, uploaded in (
        (4698, 40314, datetime(2026, 10, 10, 15, 45, 50)),
        (4700, 40316, datetime(2026, 10, 10, 16, 45, 56)),
        (4715, 40333, datetime(2026, 10, 11, 21, 55, 22)),
        (4745, 40361, datetime(2026, 10, 13, 16, 23, 26)),
    ):
        rows.append(_archive(archive_id, torrent_id, "AVC", "1", uploaded, rip_type="WEBRip"))
    for archive_id, torrent_id, uploaded in (
        (4705, 40321, datetime(2026, 10, 11, 3, 28, 52)),
        (4720, 40336, datetime(2026, 10, 12, 11, 35, 53)),
        (4748, 40364, datetime(2026, 10, 13, 17, 37, 40)),
        (4757, 40373, datetime(2026, 10, 14, 6, 3, 10)),
    ):
        rows.append(_archive(archive_id, torrent_id, "HEVC", "1", uploaded))

    file_keys = {
        row.id: content_file_keys(
            [f"Hotaru no Yomeiri/{episode:02d}.mkv" for episode in range(
                1, 4 if row.id == 4896 else 3 if row.id in (4808, 4812) else 2
            )]
        )
        for row in rows
    }
    return rows, file_keys


@pytest.mark.parametrize("include_webrip_history", [True, False])
@pytest.mark.parametrize("now", [_FROZEN_NOW, _SLA_BOUNDARY], ids=["observed", "exactly-24h"])
def test_hotaru_new_webdl_episode_has_fresh_sla(
    hotaru_snapshot, include_webrip_history: bool, now: datetime
) -> None:
    rows, file_keys = hotaru_snapshot
    if not include_webrip_history:
        rows = [row for row in rows if not row.torrent_type.startswith("WEBRip")]

    assert find_unpaired_avc(rows, now=now, file_keys_by_archive_id=file_keys) == []
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="overdue", now=now, file_keys_by_archive_id=file_keys
    ) == set()


def test_hotaru_becomes_overdue_only_after_current_webdl_sla(hotaru_snapshot) -> None:
    rows, file_keys = hotaru_snapshot
    now = _SLA_BOUNDARY + timedelta(seconds=1)
    with_history = find_unpaired_avc(rows, now=now, file_keys_by_archive_id=file_keys)
    without_history = find_unpaired_avc(
        [row for row in rows if not row.torrent_type.startswith("WEBRip")],
        now=now,
        file_keys_by_archive_id=file_keys,
    )

    assert with_history == without_history
    assert len(with_history) == 1
    item = with_history[0]
    assert item.archive_id == 4896
    assert item.release_id == _RELEASE_ID
    assert item.torrent_id == 40514
    assert item.need_state == "overdue"
    assert item.overdue is True
    assert item.missing is False
    assert item.type_mismatch is False
    assert item.created_at == _AVC_UPLOAD
    assert item.upload_created_at == _AVC_UPLOAD
    assert item.age_from_api is True
    assert item.age_hours == pytest.approx(HEVC_SLA_HOURS + 1 / 3600)
    assert item.paired_hevc_archive_id == 4812
    assert item.paired_hevc_torrent_id == 40428
    assert item.paired_hevc_info_hash == _HEVC_HASH
    assert release_ids_matching_hevc_filter(
        rows, hevc_filter="overdue", now=now, file_keys_by_archive_id=file_keys
    ) == {_RELEASE_ID}


def test_hotaru_clears_false_overdue_pipeline_need_state(
    hotaru_snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, file_keys = hotaru_snapshot
    monkeypatch.setattr(
        "app.services.hevc_pairing.load_file_keys_by_archive_id", lambda db, archives: file_keys
    )
    record = MagicMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr("app.services.pipeline.record_pipeline_event", record)
    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    db.scalar.side_effect = [
        SimpleNamespace(id=7, info_hash=_AVC_HASH, status="done"),
        SimpleNamespace(id=8, to_status="overdue", details_json={}),
    ]

    assert sync_hevc_pair_events_for_release(db, _RELEASE_ID, now=_FROZEN_NOW) == 1
    record.assert_called_once()
    assert record.call_args.kwargs["event_type"] == "hevc_status"
    assert record.call_args.kwargs["to_status"] == "ok"
    assert record.call_args.kwargs["details"]["overdue"] is False
