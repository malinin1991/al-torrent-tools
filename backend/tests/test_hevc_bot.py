import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.db.models import TelegramOutbox
from app.services.hevc_bot import (
    HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS,
    HevcReleaseStatus,
    _aggregate_file_state,
    _changes_by_release,
    aggregate_sticky_changes,
    format_msk,
    format_release_detail,
    format_release_list_item,
    query_hevc_statuses,
    query_release_detail,
    split_release_list,
    telegram_text_length,
    truncate_telegram_text,
)
from app.services.hevc_notifications import (
    enqueue_overdue_event_notifications,
    overdue_transition_dedupe_key,
)
from app.services.hevc_pairing import UnpairedAvc


def test_sticky_changes_use_exact_path_and_keep_strongest_status() -> None:
    rows = [
        SimpleNamespace(relative_path="season-a/01.mkv", ui_status="new"),
        SimpleNamespace(relative_path="season-a/01.mkv", ui_status="ok"),
        SimpleNamespace(relative_path="season-a/01.mkv", ui_status="changed"),
        SimpleNamespace(relative_path="season-b/01.mkv", ui_status="new"),
        SimpleNamespace(relative_path="season-c/02.mkv", ui_status="ok"),
    ]
    changes = aggregate_sticky_changes(rows)
    assert [(row.relative_path, row.basename, row.status) for row in changes] == [
        ("season-a/01.mkv", "01.mkv", "new"),
        ("season-b/01.mkv", "01.mkv", "new"),
        ("season-c/02.mkv", "02.mkv", "ok"),
    ]


def _hevc_archive(
    *,
    archive_id: int,
    torrent_id: int,
    info_hash: str,
    codec: str,
    created_at: datetime,
    episodes: str = "1-5",
    rip_type: str = "WEBRip",
    superseded: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=archive_id,
        release_id=7,
        torrent_id=torrent_id,
        info_hash=info_hash,
        quality_json={
            "codec": {"label": codec},
            "type": {"label": rip_type},
            "quality": {"label": "1080p"},
        },
        torrent_type=f"{rip_type} 1080p {codec}",
        torrent_description=episodes,
        created_at=created_at,
        api_created_at=created_at,
        api_present=not superseded,
        superseded=superseded,
    )


def _hevc_file(
    info_hash: str,
    path: str,
    status: str,
    torrent_id: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        release_id=7,
        torrent_id=torrent_id,
        info_hash=info_hash,
        relative_path=path,
        full_path=f"/media/{info_hash}/{path}",
        ui_status=status,
    )


def _hevc_item(
    *,
    current_archive_id: int,
    current_torrent_id: int,
    hevc_archive_id: int,
) -> UnpairedAvc:
    return UnpairedAvc(
        archive_id=current_archive_id,
        release_id=7,
        torrent_id=current_torrent_id,
        rip_family="WEBRip 1080p",
        episodes="1-5",
        created_at=datetime(2026, 1, 14),
        age_hours=48,
        missing=False,
        overdue=True,
        paired_hevc_archive_id=hevc_archive_id,
        batch_start=("regular", 1),
    )


def test_bot_status_omits_paired_movie_and_special_variants() -> None:
    """Bot использует тот же semantic pairing и не показывает ложное ожидание."""
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=101,
            info_hash="a" * 40,
            codec="AVC",
            created_at=now,
            episodes="П/м фильм",
            rip_type="BDRip",
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=102,
            info_hash="b" * 40,
            codec="HEVC",
            created_at=now,
            episodes="Movie",
            rip_type="BDRip",
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=103,
            info_hash="c" * 40,
            codec="AVC",
            created_at=now,
            episodes="Спешл",
            rip_type="BDRip",
        ),
        _hevc_archive(
            archive_id=4,
            torrent_id=104,
            info_hash="d" * 40,
            codec="HEVC",
            created_at=now,
            episodes="SPECIAL",
            rip_type="BDRip",
        ),
    ]
    db = MagicMock()
    db.scalars.side_effect = [
        MagicMock(all=lambda: archives),
        MagicMock(all=lambda: []),
        MagicMock(all=lambda: archives),
        MagicMock(all=lambda: []),
    ]

    assert query_hevc_statuses(db, kind="waiting", now=now) == []
    assert query_hevc_statuses(db, kind="overdue", now=now) == []


def _run_changes(
    archives: list[SimpleNamespace],
    files: list[SimpleNamespace],
    item: UnpairedAvc,
) -> list[tuple[str, str, str]]:
    db = MagicMock()
    db.scalars.return_value.all.return_value = files
    changes = _changes_by_release(db, [7], archives, [item])
    return [(row.relative_path, row.basename, row.status) for row in changes.get(7, [])]


def test_changes_compare_confirmed_baseline_to_current_through_full_chain() -> None:
    hevc = _hevc_archive(
        archive_id=1,
        torrent_id=200,
        info_hash="hevc",
        codec="HEVC",
        created_at=datetime(2026, 1, 2),
        episodes="1-2",
    )
    baseline = _hevc_archive(
        archive_id=2,
        torrent_id=190,
        info_hash="baseline",
        codec="AVC",
        created_at=datetime(2026, 1, 1),
        episodes="1-2",
        superseded=True,
    )
    middle = _hevc_archive(
        archive_id=3,
        torrent_id=210,
        info_hash="middle",
        codec="AVC",
        created_at=datetime(2026, 1, 7),
        episodes="1-4",
        superseded=True,
    )
    current = _hevc_archive(
        archive_id=4,
        torrent_id=220,
        info_hash="current",
        codec="AVC",
        created_at=datetime(2026, 1, 14),
    )
    other_slot = _hevc_archive(
        archive_id=5,
        torrent_id=230,
        info_hash="other-slot",
        codec="AVC",
        created_at=datetime(2026, 1, 15),
        episodes="6-7",
    )
    other_family = _hevc_archive(
        archive_id=6,
        torrent_id=240,
        info_hash="other-family",
        codec="AVC",
        created_at=datetime(2026, 1, 16),
        rip_type="WEB-DL",
    )
    archives = [hevc, baseline, middle, current, other_slot, other_family]
    files = [
        _hevc_file("baseline", "show/[01].mkv", "new", 190),
        _hevc_file("baseline", "show/[02].mkv", "new", 190),
        _hevc_file("middle", "show/[05].mkv", "new", 210),
        _hevc_file("middle", "show/[04].mkv", "new", 210),
        _hevc_file("middle", "show/[03].mkv", "new", 210),
        _hevc_file("middle", "show/[02].mkv", "ok", 210),
        _hevc_file("middle", "show/[01].mkv", "changed", 210),
        _hevc_file("current", "show/[05].mkv", "changed", 220),
        _hevc_file("current", "show/[04].mkv", "ok", 220),
        _hevc_file("current", "show/[03].mkv", "changed", 220),
        _hevc_file("current", "show/[02].mkv", "ok", 220),
        _hevc_file("current", "show/[01].mkv", "ok", 220),
        _hevc_file("other-slot", "wrong-slot/[06].mkv", "new", 230),
        _hevc_file("other-family", "wrong-family/[05].mkv", "new", 240),
    ]
    result = _run_changes(
        archives,
        files,
        _hevc_item(current_archive_id=4, current_torrent_id=220, hevc_archive_id=1),
    )
    assert result == [
        ("show/[01].mkv", "[01].mkv", "changed"),
        ("show/[02].mkv", "[02].mkv", "ok"),
        ("show/[03].mkv", "[03].mkv", "new"),
        ("show/[04].mkv", "[04].mkv", "new"),
        ("show/[05].mkv", "[05].mkv", "new"),
    ]


def test_changes_are_empty_without_avc_baseline() -> None:
    hevc = _hevc_archive(
        archive_id=1,
        torrent_id=100,
        info_hash="hevc",
        codec="HEVC",
        created_at=datetime(2026, 1, 2),
    )
    current = _hevc_archive(
        archive_id=2,
        torrent_id=200,
        info_hash="current",
        codec="AVC",
        created_at=datetime(2026, 1, 14),
    )
    files = [_hevc_file("current", "show/[01].mkv", "new", 200)]
    assert (
        _run_changes(
            [hevc, current],
            files,
            _hevc_item(current_archive_id=2, current_torrent_id=200, hevc_archive_id=1),
        )
        == []
    )


def test_changes_walk_multiple_superseded_versions() -> None:
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=105,
            info_hash="hevc",
            codec="HEVC",
            created_at=datetime(2026, 1, 2),
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=100,
            info_hash="base",
            codec="AVC",
            created_at=datetime(2026, 1, 1),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=110,
            info_hash="middle-a",
            codec="AVC",
            created_at=datetime(2026, 1, 7),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=4,
            torrent_id=110,
            info_hash="middle-b",
            codec="AVC",
            created_at=datetime(2026, 1, 8),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=5,
            torrent_id=120,
            info_hash="current",
            codec="AVC",
            created_at=datetime(2026, 1, 14),
        ),
    ]
    files = [
        _hevc_file("base", "show/[01].mkv", "new", 100),
        _hevc_file("middle-a", "show/[01].mkv", "changed", 110),
        _hevc_file("middle-b", "show/[01].mkv", "ok", 110),
        _hevc_file("current", "show/[01].mkv", "ok", 120),
    ]
    assert _run_changes(
        archives,
        files,
        _hevc_item(current_archive_id=5, current_torrent_id=120, hevc_archive_id=1),
    ) == [("show/[01].mkv", "[01].mkv", "changed")]


def test_changes_do_not_output_file_removed_from_current_avc() -> None:
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=105,
            info_hash="hevc",
            codec="HEVC",
            created_at=datetime(2026, 1, 2),
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=100,
            info_hash="base",
            codec="AVC",
            created_at=datetime(2026, 1, 1),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=120,
            info_hash="current",
            codec="AVC",
            created_at=datetime(2026, 1, 14),
        ),
    ]
    files = [
        _hevc_file("base", "show/removed.mkv", "new", 100),
        _hevc_file("base", "show/present.mkv", "new", 100),
        _hevc_file("current", "show/present.mkv", "ok", 120),
    ]
    assert _run_changes(
        archives,
        files,
        _hevc_item(current_archive_id=3, current_torrent_id=120, hevc_archive_id=1),
    ) == [("show/present.mkv", "present.mkv", "ok")]


def test_changes_readded_baseline_path_is_new_not_ok() -> None:
    """Файл был в HEVC baseline, исчез и вернулся как new — итог new, не ok."""
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=105,
            info_hash="hevc",
            codec="HEVC",
            created_at=datetime(2026, 1, 2),
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=100,
            info_hash="base",
            codec="AVC",
            created_at=datetime(2026, 1, 1),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=110,
            info_hash="middle",
            codec="AVC",
            created_at=datetime(2026, 1, 8),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=4,
            torrent_id=120,
            info_hash="current",
            codec="AVC",
            created_at=datetime(2026, 1, 14),
        ),
    ]
    files = [
        _hevc_file("base", "show/returned.mkv", "new", 100),
        _hevc_file("base", "show/stayed.mkv", "new", 100),
        _hevc_file("middle", "show/stayed.mkv", "ok", 110),
        _hevc_file("current", "show/returned.mkv", "new", 120),
        _hevc_file("current", "show/stayed.mkv", "ok", 120),
    ]
    assert _run_changes(
        archives,
        files,
        _hevc_item(current_archive_id=4, current_torrent_id=120, hevc_archive_id=1),
    ) == [
        ("show/returned.mkv", "returned.mkv", "new"),
        ("show/stayed.mkv", "stayed.mkv", "ok"),
    ]


def test_changes_keep_equal_basenames_at_distinct_exact_paths() -> None:
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=105,
            info_hash="hevc",
            codec="HEVC",
            created_at=datetime(2026, 1, 2),
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=100,
            info_hash="base",
            codec="AVC",
            created_at=datetime(2026, 1, 1),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=120,
            info_hash="current",
            codec="AVC",
            created_at=datetime(2026, 1, 14),
        ),
    ]
    files = [
        _hevc_file("base", "season-a/01.mkv", "new", 100),
        _hevc_file("base", "season-b/01.mkv", "new", 100),
        _hevc_file("current", "season-a/01.mkv", "changed", 120),
        _hevc_file("current", "season-b/01.mkv", "ok", 120),
    ]
    assert _run_changes(
        archives,
        files,
        _hevc_item(current_archive_id=3, current_torrent_id=120, hevc_archive_id=1),
    ) == [
        ("season-a/01.mkv", "01.mkv", "changed"),
        ("season-b/01.mkv", "01.mkv", "ok"),
    ]


def _checking_file_state(
    active_hashes: set[str],
) -> tuple[list, bool]:
    archives = [
        _hevc_archive(
            archive_id=1,
            torrent_id=105,
            info_hash="hevc",
            codec="HEVC",
            created_at=datetime(2026, 1, 2),
        ),
        _hevc_archive(
            archive_id=2,
            torrent_id=100,
            info_hash="base",
            codec="AVC",
            created_at=datetime(2026, 1, 1),
            superseded=True,
        ),
        _hevc_archive(
            archive_id=3,
            torrent_id=120,
            info_hash="current",
            codec="AVC",
            created_at=datetime(2026, 1, 14),
        ),
    ]
    files = [
        _hevc_file("hevc", "show/hevc.mkv", "changed", 105),
        _hevc_file("base", "show/01.mkv", "changed", 100),
        _hevc_file("current", "show/01.mkv", "changed", 120),
        _hevc_file("current", "show/02.mkv", "new", 120),
    ]
    changes, checking_ids = _aggregate_file_state(
        archives,
        files,
        [_hevc_item(current_archive_id=3, current_torrent_id=120, hevc_archive_id=1)],
        active_hashes=active_hashes,
        disk_hashes_by_path={},
    )
    return changes[7], 7 in checking_ids


def test_current_avc_checking_hides_partial_changes_with_exact_message() -> None:
    changes, checking = _checking_file_state({"current"})
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        changes=changes,
        changes_checking=checking,
    )

    text = format_release_detail(row)
    message = (
        "Изменения файлов после HEVC (sticky) появятся позже, "
        "так как новые файлы ещё не проверены."
    )
    assert checking is True
    assert message in text
    assert "Изменения файлов после HEVC (sticky):" not in text
    assert "01.mkv — changed" not in text
    assert "02.mkv — new" not in text


def test_checking_in_historical_or_hevc_does_not_hide_current_changes() -> None:
    changes, checking = _checking_file_state({"base", "hevc"})
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        changes=changes,
        changes_checking=checking,
    )

    text = format_release_detail(row)
    assert checking is False
    assert "Изменения файлов после HEVC (sticky):" in text
    assert "✏️ 01.mkv — changed" in text
    assert "➕ 02.mkv — new" in text
    assert "появятся позже" not in text


def test_changes_return_automatically_after_current_avc_settles() -> None:
    changes, checking = _checking_file_state(set())
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        changes=changes,
        changes_checking=checking,
    )

    text = format_release_detail(row)
    assert checking is False
    assert "✏️ 01.mkv — changed" in text
    assert "➕ 02.mkv — new" in text
    assert "появятся позже" not in text


def test_detail_shows_only_new_and_changed_file_changes() -> None:
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show <test>",
        original_title="Original & title",
        executors=[],
        items=[
            UnpairedAvc(
                archive_id=1,
                release_id=7,
                torrent_id=70,
                rip_family="WEBRip 1080p",
                episodes="1-2",
                created_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
                age_hours=30,
                missing=False,
                overdue=False,
                type_mismatch=True,
            )
        ],
        changes=aggregate_sticky_changes(
            [
                SimpleNamespace(relative_path="dir/a&b.mkv", ui_status="changed"),
                SimpleNamespace(relative_path="dir/new.mkv", ui_status="new"),
                SimpleNamespace(relative_path="dir/ready.mkv", ui_status="ok"),
            ]
        ),
    )
    text = format_release_detail(row)
    assert "Show &lt;test&gt;" in text
    assert "Original &amp; title" in text
    assert "26.08.2026 12:00 MSK" in text
    assert "расхождение типов" in text
    assert "a&amp;b.mkv" in text
    assert "✏️ a&amp;b.mkv — changed" in text
    assert "➕ new.mkv — new" in text
    assert "ready.mkv" not in text
    assert " — ok" not in text
    assert "Изменения файлов после HEVC (sticky)" in text
    assert "Статусы файлов после HEVC (sticky)" not in text
    assert "Исполнители: —" in text


def test_detail_reports_no_file_changes_when_only_ok_remains() -> None:
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        changes=aggregate_sticky_changes(
            [SimpleNamespace(relative_path="dir/ready.mkv", ui_status="ok")]
        ),
    )

    text = format_release_detail(row)
    assert "Изменений файлов после HEVC нет." in text
    assert "Изменения файлов после HEVC (sticky)" not in text
    assert "ready.mkv" not in text


def test_detail_does_not_claim_hevc_pair_when_no_avc_needs_hevc() -> None:
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        items=[],
    )
    text = format_release_detail(row)
    assert "Нет AVC, требующих HEVC" in text
    assert "Актуальная HEVC-пара" not in text


def test_detail_formats_ignored_avc_explicitly() -> None:
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        items=[
            UnpairedAvc(
                archive_id=1,
                release_id=7,
                torrent_id=70,
                rip_family="WEBRip 720p",
                episodes="1-2",
                created_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
                age_hours=30,
                missing=True,
                overdue=False,
                ignore_hevc=True,
            )
        ],
    )
    text = format_release_detail(row)
    assert "игнор HEVC" in text
    assert "Актуальная HEVC-пара" not in text
    assert "просрочка" not in text


def test_long_lists_split_with_matching_detail_rows() -> None:
    rows = [
        HevcReleaseStatus(
            release_id=index,
            alias=f"show-{index}",
            title="Очень длинное название " * 8,
            original_title=None,
            executors=["Coder"],
            items=[],
        )
        for index in range(1, 10)
    ]
    chunks = split_release_list(rows, kind="waiting", max_len=500)
    assert len(chunks) > 1
    assert [row.release_id for _, part in chunks for row in part] == list(range(1, 10))
    assert all(len(text) <= 500 for text, _ in chunks)


def test_type_mismatch_has_dedicated_error_list_format() -> None:
    row = HevcReleaseStatus(
        release_id=1,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        items=[
            UnpairedAvc(
                archive_id=1,
                release_id=1,
                torrent_id=1,
                rip_family="WEBRip 1080p",
                episodes="1",
                created_at=None,
                age_hours=50,
                missing=False,
                overdue=False,
                type_mismatch=True,
            )
        ],
    )
    text = format_release_list_item(row, kind="error")
    assert "расхождение типов AVC/HEVC" in text
    assert "https://anilibria.top/anime/releases/release/show" in text
    assert "неизвестно" in text
    assert "просрочка" not in text


def test_error_list_empty_case() -> None:
    [(text, rows)] = split_release_list([], kind="error")
    assert "<b>Ошибки:</b>" in text
    assert "Расхождений типов AVC/HEVC нет." in text
    assert rows == []


def _pending_status_item(
    *,
    archive_id: int,
    release_id: int = 7,
    age_hours: float,
    missing: bool = True,
    type_mismatch: bool = False,
) -> UnpairedAvc:
    return UnpairedAvc(
        archive_id=archive_id,
        release_id=release_id,
        torrent_id=archive_id * 10,
        rip_family="WEBRip 1080p",
        episodes="1-2",
        created_at=datetime(2026, 7, 1),
        age_hours=age_hours,
        missing=missing,
        overdue=False,
        type_mismatch=type_mismatch,
    )


def _mock_status_rows(monkeypatch, items: list[UnpairedAvc]) -> None:
    release_ids = sorted({item.release_id for item in items})
    archives = [
        SimpleNamespace(
            id=release_id,
            release_id=release_id,
            torrent_id=release_id * 100,
            release_alias=f"show-{release_id}",
            anime_name=f"Show {release_id}",
        )
        for release_id in release_ids
    ]
    releases = {
        release_id: SimpleNamespace(
            release_id=release_id,
            release_alias=f"show-{release_id}",
            title=f"Show {release_id}",
            original_title=None,
        )
        for release_id in release_ids
    }
    monkeypatch.setattr(
        "app.services.hevc_bot._load_release_rows",
        lambda db: (archives, releases),
    )
    monkeypatch.setattr(
        "app.services.hevc_bot.find_unpaired_avc",
        lambda *args, **kwargs: items,
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._executors_by_release",
        lambda db, ids: {},
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._changes_by_release",
        lambda db, ids, archives, selected: {},
    )


def test_status_list_keeps_missing_at_29d_23h_59m(monkeypatch) -> None:
    item = _pending_status_item(
        archive_id=1,
        age_hours=29 * 24 + 23 + 59 / 60,
    )
    _mock_status_rows(monkeypatch, [item])

    rows = query_hevc_statuses(MagicMock(), kind="waiting")

    assert [row.release_id for row in rows] == [7]
    assert rows[0].items == [item]


def test_status_list_hides_missing_at_exactly_30d(monkeypatch) -> None:
    item = _pending_status_item(
        archive_id=1,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS,
    )
    _mock_status_rows(monkeypatch, [item])

    assert query_hevc_statuses(MagicMock(), kind="waiting") == []


def test_status_list_hides_missing_older_than_30d(monkeypatch) -> None:
    item = _pending_status_item(
        archive_id=1,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS + 1,
    )
    _mock_status_rows(monkeypatch, [item])

    assert query_hevc_statuses(MagicMock(), kind="waiting") == []


def test_release_detail_keeps_missing_older_than_30d(monkeypatch) -> None:
    item = _pending_status_item(
        archive_id=1,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS + 1,
    )
    _mock_status_rows(monkeypatch, [item])

    row = query_release_detail(MagicMock(), 7)

    assert row is not None
    assert row.items == [item]
    assert "HEVC отсутствует" in format_release_detail(row)


def test_release_detail_ignored_only_avc_uses_neutral_text(monkeypatch) -> None:
    archive = SimpleNamespace(
        id=1,
        release_id=7,
        torrent_id=70,
        release_alias="show",
        anime_name="Show",
        ignore_hevc=True,
    )
    release = SimpleNamespace(
        release_id=7,
        release_alias="show",
        title="Show",
        original_title=None,
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._load_release_rows",
        lambda db: ([archive], {7: release}),
    )
    monkeypatch.setattr(
        "app.services.hevc_bot.find_unpaired_avc",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._executors_by_release",
        lambda db, ids: {},
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._file_state_by_release",
        lambda db, ids, archives, items: ({}, set()),
    )

    row = query_release_detail(MagicMock(), 7)

    assert row is not None
    assert row.items == []
    text = format_release_detail(row)
    assert "Нет AVC, требующих HEVC" in text
    assert "Актуальная HEVC-пара" not in text


def test_status_list_filters_old_missing_items_per_item_in_mixed_release(
    monkeypatch,
) -> None:
    old_missing = _pending_status_item(
        archive_id=1,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS,
    )
    fresh_missing = _pending_status_item(
        archive_id=2,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS - 1,
    )
    mismatch = _pending_status_item(
        archive_id=3,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS + 100,
        missing=False,
        type_mismatch=True,
    )
    old_only = _pending_status_item(
        archive_id=4,
        release_id=8,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS + 1,
    )
    _mock_status_rows(
        monkeypatch,
        [old_missing, fresh_missing, mismatch, old_only],
    )

    rows = query_hevc_statuses(MagicMock(), kind="waiting")

    assert [row.release_id for row in rows] == [7]
    assert [item.archive_id for item in rows[0].items] == [2]


def test_error_list_contains_only_type_mismatch(monkeypatch) -> None:
    waiting = _pending_status_item(archive_id=1, age_hours=5, missing=False)
    mismatch = _pending_status_item(
        archive_id=2,
        age_hours=HEVC_MISSING_STATUS_LIST_MAX_AGE_HOURS + 100,
        missing=False,
        type_mismatch=True,
    )
    other_mismatch = _pending_status_item(
        archive_id=3,
        release_id=8,
        age_hours=1,
        missing=False,
        type_mismatch=True,
    )
    _mock_status_rows(monkeypatch, [waiting, mismatch, other_mismatch])

    rows = query_hevc_statuses(MagicMock(), kind="error")

    assert [row.release_id for row in rows] == [7, 8]
    assert [item.archive_id for row in rows for item in row.items] == [2, 3]


def test_overdue_nickname_filter_is_exact_and_case_insensitive(monkeypatch) -> None:
    item = UnpairedAvc(
        archive_id=1,
        release_id=7,
        torrent_id=70,
        rip_family="WEBRip 1080p",
        episodes="1-2",
        created_at=datetime(2026, 8, 25, 0, 0),
        age_hours=30,
        missing=False,
        overdue=True,
    )
    archive = SimpleNamespace(
        id=1,
        release_id=7,
        torrent_id=70,
        release_alias="show",
        anime_name="Show",
        quality_json={"codec": {"label": "AVC"}},
        torrent_type="WEBRip 1080p AVC",
    )
    release = SimpleNamespace(
        release_id=7,
        release_alias="show",
        title="Show",
        original_title="Original",
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._load_release_rows",
        lambda db: ([archive], {7: release}),
    )
    monkeypatch.setattr(
        "app.services.hevc_bot.find_unpaired_avc",
        lambda *args, **kwargs: [item],
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._executors_by_release",
        lambda db, ids: {7: ["ExactCoder"]},
    )
    monkeypatch.setattr(
        "app.services.hevc_bot._file_state_by_release",
        lambda db, ids, archives, items: ({}, set()),
    )

    assert [
        row.release_id
        for row in query_hevc_statuses(
            MagicMock(), kind="overdue", nickname="exactcoder"
        )
    ] == [7]
    assert query_hevc_statuses(MagicMock(), kind="overdue", nickname="Exact") == []


def test_overdue_notification_routes_to_hevc_groups_with_transition_dedupe(
    monkeypatch,
) -> None:
    detail = HevcReleaseStatus(
        release_id=9,
        alias="show",
        title="Show",
        original_title=None,
        executors=["Coder"],
        items=[],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.list_approved_group_ids",
        lambda db, bot_key: [-1001, -1002],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.query_release_detail",
        lambda db, release_id: detail,
    )
    db = MagicMock()
    db.scalar.return_value = None
    event = SimpleNamespace(
        id=55,
        pipeline_id=3,
        event_type="hevc_status",
        from_status="missing",
        to_status="overdue",
        details_json={
            "release_id": 9,
            "info_hash": "AA" * 20,
            "prev_hevc_status_event_id": 10,
        },
    )

    assert enqueue_overdue_event_notifications(db, event) == 2
    outboxes = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], TelegramOutbox)
    ]
    assert [row.bot_key for row in outboxes] == ["hevc", "hevc"]
    assert [row.dedupe_key for row in outboxes] == [
        overdue_transition_dedupe_key(
            release_id=9, info_hash="aa" * 20, prev_event_id=10, chat_id=-1001
        ),
        overdue_transition_dedupe_key(
            release_id=9, info_hash="aa" * 20, prev_event_id=10, chat_id=-1002
        ),
    ]
    assert all(row.payload_json["reply_markup"] for row in outboxes)
    db.commit.assert_called_once()


def test_overdue_notification_ignores_non_overdue_event() -> None:
    db = MagicMock()
    event = SimpleNamespace(
        event_type="hevc_status",
        to_status="missing",
        details_json={"release_id": 9},
    )
    assert enqueue_overdue_event_notifications(db, event) == 0
    db.add.assert_not_called()


def test_overdue_notification_ignores_type_mismatch_event() -> None:
    db = MagicMock()
    event = SimpleNamespace(
        event_type="hevc_status",
        to_status="type_mismatch",
        details_json={"release_id": 9},
    )
    assert enqueue_overdue_event_notifications(db, event) == 0
    db.add.assert_not_called()


def test_overdue_notification_skips_still_overdue_flag_change() -> None:
    db = MagicMock()
    event = SimpleNamespace(
        event_type="hevc_status",
        from_status="overdue",
        to_status="overdue",
        details_json={"release_id": 9, "info_hash": "aa" * 20},
    )
    assert enqueue_overdue_event_notifications(db, event) == 0
    db.add.assert_not_called()


def test_format_msk_accepts_naive_utc() -> None:
    assert format_msk(datetime(2026, 8, 26, 0, 0)) == "26.08.2026 03:00 MSK"


def test_detail_uses_active_avc_upload_time_not_historical_sla_anchor() -> None:
    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
        items=[
            UnpairedAvc(
                archive_id=1,
                release_id=7,
                torrent_id=70,
                rip_family="WEBRip 1080p",
                episodes="1-2",
                created_at=datetime(2026, 8, 20, 0, 0),
                age_hours=150,
                missing=False,
                overdue=True,
                upload_created_at=datetime(2026, 8, 26, 0, 0),
            )
        ],
    )
    text = format_release_detail(row)
    assert "Загружен: 26.08.2026 03:00 MSK" in text
    assert "20.08.2026" not in text


def test_telegram_limits_count_utf16_and_preserve_whole_html_lines() -> None:
    assert telegram_text_length("😀") == 2
    assert telegram_text_length(truncate_telegram_text("😀" * 40, 64)) <= 64
    row = HevcReleaseStatus(
        release_id=1,
        alias="show",
        title="Show",
        original_title="😀" * 200,
        executors=[],
        changes=aggregate_sticky_changes(
            [SimpleNamespace(relative_path="dir/" + "😀" * 5000, ui_status="changed")]
        ),
    )
    text = format_release_detail(row, max_len=700)
    assert telegram_text_length(text) <= 700
    assert text.endswith("…")
    assert text.count("<b>") == text.count("</b>")


def test_notification_is_queued_even_while_bot_disabled_and_fits_limit(
    monkeypatch,
) -> None:
    detail = HevcReleaseStatus(
        release_id=9,
        alias="show",
        title="Show",
        original_title="😀" * 1800,
        executors=["Coder"],
        items=[],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.list_approved_group_ids",
        lambda db, bot_key: [-1001],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.query_release_detail",
        lambda db, release_id: detail,
    )
    db = MagicMock()
    db.scalar.return_value = None
    event = SimpleNamespace(
        id=56,
        pipeline_id=3,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json={"release_id": 9, "info_hash": "bb" * 20},
    )

    assert enqueue_overdue_event_notifications(db, event) == 1
    outbox = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], TelegramOutbox)
    )
    assert telegram_text_length(outbox.payload_json["text"]) <= 4096


def test_parallel_overdue_sync_shares_transition_key(monkeypatch) -> None:
    """Два event.id одного перехода overdue не дают два outbox в ту же группу."""
    detail = HevcReleaseStatus(
        release_id=9,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.list_approved_group_ids",
        lambda db, bot_key: [-1001],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.query_release_detail",
        lambda db, release_id: detail,
    )
    db = MagicMock()
    seen_keys: set[str] = set()

    def scalar(statement):  # noqa: ANN001
        compile_state = str(statement)
        _ = compile_state
        # MagicMock SELECT: считаем, что ключ уже есть после первой вставки.
        if seen_keys:
            return 1
        return None

    def add(row):  # noqa: ANN001
        if isinstance(row, TelegramOutbox) and row.dedupe_key:
            seen_keys.add(row.dedupe_key)

    db.scalar.side_effect = scalar
    db.add.side_effect = add
    details = {
        "release_id": 9,
        "info_hash": "cc" * 20,
        "prev_hevc_status_event_id": 4,
    }
    first = SimpleNamespace(
        id=80,
        pipeline_id=3,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json=details,
    )
    second = SimpleNamespace(
        id=81,
        pipeline_id=3,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json=details,
    )
    assert enqueue_overdue_event_notifications(db, first) == 1
    assert enqueue_overdue_event_notifications(db, second) == 0
    assert seen_keys == {
        overdue_transition_dedupe_key(
            release_id=9, info_hash="cc" * 20, prev_event_id=4, chat_id=-1001
        )
    }


def test_each_new_overdue_avc_gets_its_own_dedupe_key(monkeypatch) -> None:
    detail = HevcReleaseStatus(
        release_id=9,
        alias="show",
        title="Show",
        original_title=None,
        executors=[],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.list_approved_group_ids",
        lambda db, bot_key: [-1001],
    )
    monkeypatch.setattr(
        "app.services.hevc_notifications.query_release_detail",
        lambda db, release_id: detail,
    )
    db = MagicMock()
    db.scalar.return_value = None
    first = SimpleNamespace(
        id=60,
        pipeline_id=60,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json={
            "release_id": 9,
            "info_hash": "dd" * 20,
            "prev_hevc_status_event_id": 1,
        },
    )
    second = SimpleNamespace(
        id=61,
        pipeline_id=61,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json={
            "release_id": 9,
            "info_hash": "ee" * 20,
            "prev_hevc_status_event_id": 1,
        },
    )
    third = SimpleNamespace(
        id=62,
        pipeline_id=60,
        event_type="hevc_status",
        from_status="ok",
        to_status="overdue",
        details_json={
            "release_id": 9,
            "info_hash": "dd" * 20,
            "prev_hevc_status_event_id": 50,
        },
    )
    assert enqueue_overdue_event_notifications(db, first) == 1
    assert enqueue_overdue_event_notifications(db, second) == 1
    assert enqueue_overdue_event_notifications(db, third) == 1
    keys = [
        call.args[0].dedupe_key
        for call in db.add.call_args_list
        if isinstance(call.args[0], TelegramOutbox)
    ]
    assert keys == [
        overdue_transition_dedupe_key(
            release_id=9, info_hash="dd" * 20, prev_event_id=1, chat_id=-1001
        ),
        overdue_transition_dedupe_key(
            release_id=9, info_hash="ee" * 20, prev_event_id=1, chat_id=-1001
        ),
        overdue_transition_dedupe_key(
            release_id=9, info_hash="dd" * 20, prev_event_id=50, chat_id=-1001
        ),
    ]


def test_hevc_profile_registers_group_membership_handler() -> None:
    from telegram.ext import ChatMemberHandler

    from app.telegram_bot.__main__ import _register_handlers

    application = MagicMock()
    _register_handlers(application, "hevc")
    handlers = [call.args[0] for call in application.add_handler.call_args_list]
    assert any(isinstance(handler, ChatMemberHandler) for handler in handlers)


def test_group_addition_runs_auto_pending_access(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    check = AsyncMock(return_value=False)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", check)
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(new_chat_member=SimpleNamespace(status="member"))
    )
    context = MagicMock()
    asyncio.run(hevc_handlers.group_membership(update, context))
    check.assert_awaited_once_with(update, context)


def test_status_callback_rechecks_acl_before_details(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=False)
    send_detail = AsyncMock()
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(hevc_handlers, "_send_detail", send_detail)
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data="status:7"),
    )
    context = MagicMock()
    asyncio.run(hevc_handlers.status_callback(update, context))
    access.assert_awaited_once_with(update)
    send_detail.assert_not_awaited()


def test_error_rechecks_acl_before_query(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=False)
    query = MagicMock()
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(hevc_handlers, "query_hevc_statuses", query)
    update, reply = _message_update("/error", "private")

    asyncio.run(hevc_handlers.error(update, SimpleNamespace(args=[])))

    access.assert_awaited_once_with(update)
    query.assert_not_called()
    reply.assert_not_awaited()


def test_error_list_has_detail_button_and_shared_callback(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    row = HevcReleaseStatus(
        release_id=7,
        alias="show",
        title="Show",
        original_title=None,
        executors=["Coder"],
        items=[_pending_status_item(archive_id=1, age_hours=2, type_mismatch=True)],
    )
    monkeypatch.setattr(
        hevc_handlers, "check_hevc_access", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        hevc_handlers, "query_hevc_statuses", MagicMock(return_value=[row])
    )
    monkeypatch.setattr(hevc_handlers, "SessionLocal", MagicMock())
    update, reply = _message_update("/error", "private")

    asyncio.run(hevc_handlers.error(update, SimpleNamespace(args=[])))

    kwargs = reply.await_args.kwargs
    assert "Ошибки:" in reply.await_args.args[0]
    assert "расхождение типов AVC/HEVC" in reply.await_args.args[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].text.startswith("Детали")
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "status:7"

    send_detail = AsyncMock()
    monkeypatch.setattr(hevc_handlers, "_send_detail", send_detail)
    callback_update = SimpleNamespace(
        callback_query=SimpleNamespace(data="status:7"),
    )
    asyncio.run(
        hevc_handlers.status_callback(callback_update, SimpleNamespace(args=[]))
    )
    send_detail.assert_awaited_once_with(callback_update, 7)


def test_start_and_help_list_error_on_separate_line(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    monkeypatch.setattr(
        hevc_handlers, "check_hevc_access", AsyncMock(return_value=True)
    )
    for command in ("/start", "/help"):
        update, reply = _message_update(command, "private")
        asyncio.run(hevc_handlers.start(update, SimpleNamespace(args=[])))
        lines = reply.await_args.args[0].splitlines()
        assert "/error — расхождения типов AVC/HEVC" in lines
        assert "/status <release_id> — детали релиза" in lines


def _message_update(text: str, chat_type: str) -> tuple[SimpleNamespace, AsyncMock]:
    reply = AsyncMock()
    update = SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=reply),
        effective_chat=SimpleNamespace(type=chat_type),
    )
    return update, reply


def _bot_context(username: str | None = "ActualHevcBot") -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            username=username,
            get_me=AsyncMock(return_value=SimpleNamespace(username="ActualHevcBot")),
        )
    )


def test_random_replies_contain_exactly_27_unquoted_phrases() -> None:
    from app.telegram_bot.hevc_handlers import RANDOM_REPLIES

    assert len(RANDOM_REPLIES) == 27
    assert len(set(RANDOM_REPLIES)) == 27
    assert (
        "Хм, 48x48? Уже достаёшь ту самую аудиодорожку? Смело... и очень самоуверенно 😼"
        in RANDOM_REPLIES
    )
    assert (
        "Фокси, тебе сейчас перепадёт. И не потому что я злая, а потому что ты "
        "слишком милый, когда бесишься 😈" in RANDOM_REPLIES
    )
    assert all(phrase == phrase.strip() for phrase in RANDOM_REPLIES)
    assert all(
        not phrase.startswith(('"', "'", "«", "“"))
        and not phrase.endswith(('"', "'", "»", "”"))
        for phrase in RANDOM_REPLIES
    )


def test_group_mention_uses_runtime_bot_username_and_random_choice(
    monkeypatch,
) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=True)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(
        hevc_handlers.random,
        "choice",
        lambda phrases: phrases[4],
    )
    update, reply = _message_update("Привет, @ActualHevcBot!", "group")
    context = _bot_context(username=None)

    asyncio.run(hevc_handlers.addressed_text(update, context))

    context.bot.get_me.assert_awaited_once()
    access.assert_awaited_once_with(update)
    reply.assert_awaited_once_with(hevc_handlers.RANDOM_REPLIES[4])


def test_unknown_command_addressed_to_bot_gets_random_reply(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=True)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(
        hevc_handlers.random,
        "choice",
        lambda phrases: phrases[8],
    )
    update, reply = _message_update("/unknown@ActualHevcBot argument", "group")

    asyncio.run(hevc_handlers.unknown_command(update, _bot_context()))

    access.assert_awaited_once_with(update)
    reply.assert_awaited_once_with(hevc_handlers.RANDOM_REPLIES[8])


def test_private_text_gets_random_reply(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=True)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(
        hevc_handlers.random,
        "choice",
        lambda phrases: phrases[-1],
    )
    update, reply = _message_update("Просто текст", "private")

    asyncio.run(hevc_handlers.addressed_text(update, _bot_context()))

    access.assert_awaited_once_with(update)
    reply.assert_awaited_once_with(hevc_handlers.RANDOM_REPLIES[-1])


def test_private_unknown_command_gets_random_reply(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=True)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    monkeypatch.setattr(
        hevc_handlers.random,
        "choice",
        lambda phrases: phrases[2],
    )
    update, reply = _message_update("/something", "private")

    asyncio.run(hevc_handlers.unknown_command(update, _bot_context()))

    access.assert_awaited_once_with(update)
    reply.assert_awaited_once_with(hevc_handlers.RANDOM_REPLIES[2])


def test_plain_group_text_is_ignored_before_acl(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=True)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    update, reply = _message_update("Обычный разговор", "supergroup")

    asyncio.run(hevc_handlers.addressed_text(update, _bot_context()))

    access.assert_not_awaited()
    reply.assert_not_awaited()


def test_random_reply_handlers_obey_acl(monkeypatch) -> None:
    from app.telegram_bot import hevc_handlers

    access = AsyncMock(return_value=False)
    monkeypatch.setattr(hevc_handlers, "check_hevc_access", access)
    mention, mention_reply = _message_update("@ActualHevcBot привет", "group")
    command, command_reply = _message_update("/unknown", "private")

    asyncio.run(hevc_handlers.addressed_text(mention, _bot_context()))
    asyncio.run(hevc_handlers.unknown_command(command, _bot_context()))

    assert access.await_count == 2
    mention_reply.assert_not_awaited()
    command_reply.assert_not_awaited()


def test_random_handlers_are_registered_after_supported_commands() -> None:
    from app.telegram_bot.__main__ import _register_handlers

    application = MagicMock()
    _register_handlers(application, "hevc")
    handlers = [call.args[0] for call in application.add_handler.call_args_list]
    callbacks = [handler.callback.__name__ for handler in handlers]

    supported_indexes = [
        callbacks.index(name) for name in ("start", "overdue", "status", "error")
    ]
    assert any(
        getattr(handler, "commands", set()) == frozenset({"error"})
        for handler in handlers
    )
    assert callbacks.index("unknown_command") > max(supported_indexes)
    assert callbacks.index("addressed_text") > callbacks.index("unknown_command")
