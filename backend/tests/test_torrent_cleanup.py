from types import SimpleNamespace

from app.db.models import CleanupRule
from app.services.torrent_cleanup import find_removable_torrents


def build_rule(
    *,
    tracker_host: str = "tr.libria.fun",
    message_contains: str = "Торрент не зарегистрирован",
    include_errored: bool = True,
    delete_files: bool = False,
) -> CleanupRule:
    return CleanupRule(
        name="Тестовое правило",
        tracker_host=tracker_host,
        message_contains=message_contains,
        include_errored=include_errored,
        delete_files=delete_files,
        target_client="master",
        enabled=True,
    )


def test_find_removable_torrents_matches_tracker_and_merges_delete_flag() -> None:
    torrent = SimpleNamespace(
        hash="ABC123",
        name="Example torrent",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=5,
                url="http://tr.libria.fun:2710/announce",
                msg="Торрент не зарегистрирован на трекере",
            )
        ],
    )

    removable = find_removable_torrents(
        torrent_list=[torrent],
        rules=[build_rule(), build_rule(delete_files=True)],
    )

    assert removable == [
        {
            "hash": "abc123",
            "name": "Example torrent",
            "delete_files": True,
            "reason": "tracker",
        }
    ]


def test_find_removable_torrents_matches_host_without_port() -> None:
    torrent = SimpleNamespace(
        hash="abc",
        name="Libria",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=5,
                url="http://tr.libria.fun:2710/announce",
                msg="Торрент не зарегистрирован",
            )
        ],
    )

    removable = find_removable_torrents(
        torrent_list=[torrent],
        rules=[build_rule(tracker_host="tr.libria.fun")],
    )

    assert len(removable) == 1
    assert removable[0]["reason"] == "tracker"


def test_find_removable_ignores_not_working_status_without_tracker_error() -> None:
    """NotWorking (4) без TrackerError не удаляем — только status=5 + msg."""
    torrent = SimpleNamespace(
        hash="notworking",
        name="Not working only",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=4,
                url="http://tr.libria.fun:2710/announce",
                msg="Торрент не зарегистрирован",
            )
        ],
    )

    assert find_removable_torrents(torrent_list=[torrent], rules=[build_rule()]) == []


def test_find_removable_torrents_matches_msg_on_endpoint() -> None:
    torrent = SimpleNamespace(
        hash="endpoint1",
        name="Endpoint msg",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=[
            SimpleNamespace(
                status=5,
                url="http://tr.libria.fun:2710/announce",
                msg="",
                endpoints=[
                    SimpleNamespace(
                        status=5,
                        msg="Торрент не зарегистрирован",
                    )
                ],
            )
        ],
    )

    removable = find_removable_torrents(torrent_list=[torrent], rules=[build_rule()])

    assert len(removable) == 1


def test_find_removable_torrents_reads_trackers_data_attr() -> None:
    """Как в старом скрипте: torrent.trackers.data."""
    torrent = SimpleNamespace(
        hash="def",
        name="With .data",
        state_enum=SimpleNamespace(is_errored=False),
        trackers=SimpleNamespace(
            data=[
                SimpleNamespace(
                    status=5,
                    url="udp://tr.libria.fun:2710/announce",
                    msg="Торрент не зарегистрирован",
                )
            ]
        ),
    )

    removable = find_removable_torrents(torrent_list=[torrent], rules=[build_rule()])

    assert len(removable) == 1


def test_find_removable_torrents_matches_errored_state() -> None:
    torrent = SimpleNamespace(
        hash="deadbeef",
        name="Broken torrent",
        state_enum=SimpleNamespace(is_errored=True),
        trackers=[
            SimpleNamespace(
                status=1,
                url="http://tr.libria.fun:2710/announce",
                msg="",
            )
        ],
    )

    removable = find_removable_torrents(torrent_list=[torrent], rules=[build_rule(include_errored=True)])

    assert removable == [
        {
            "hash": "deadbeef",
            "name": "Broken torrent",
            "delete_files": False,
            "reason": "errored",
        }
    ]


def test_find_removable_torrents_ignores_errored_without_tracker_host() -> None:
    torrent = SimpleNamespace(
        hash="cafebabe",
        name="Other tracker errored",
        state_enum=SimpleNamespace(is_errored=True),
        trackers=[
            SimpleNamespace(
                status=1,
                url="http://other.tracker.example/announce",
                msg="error",
            )
        ],
    )

    removable = find_removable_torrents(torrent_list=[torrent], rules=[build_rule(include_errored=True)])

    assert removable == []
