"""E2E: freshness по torrent_id, ignore_hevc, multi-torrent sibling removed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.hevc_pairing import (
    HEVC_SLA_HOURS,
    find_unpaired_avc,
    release_ids_matching_hevc_filter,
)
from app.services.releases_view import (
    _build_file_rows,
    _filter_removed_candidates,
)


def _qj(*, rip_type: str = "BDRip", quality: str = "1080p", codec: str) -> dict:
    return {
        "type": {"value": rip_type},
        "quality": {"value": quality},
        "codec": {"label": codec, "value": f"x/{codec}"},
    }


def _row(
    *,
    archive_id: int,
    release_id: int = 1,
    torrent_id: int,
    episodes: str = "1-12",
    codec: str,
    rip_type: str = "BDRip",
    created_at: datetime | None = None,
    api_created_at: datetime | None = None,
    api_present: bool = True,
    superseded: bool = False,
    ignore_hevc: bool = False,
    info_hash: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=archive_id,
        release_id=release_id,
        torrent_id=torrent_id,
        torrent_description=episodes,
        torrent_type=f"{rip_type} 1080p {codec}",
        quality_json=_qj(rip_type=rip_type, codec=codec),
        created_at=created_at,
        api_created_at=api_created_at,
        api_present=api_present,
        superseded=superseded,
        ignore_hevc=ignore_hevc,
        info_hash=info_hash or f"{archive_id:040x}",
    )


# --- 1–2: freshness по AniLibria torrent_id ---


def test_hevc_higher_torrent_id_not_overdue_despite_earlier_created_at() -> None:
    """HEVC ingest раньше AVC в ALTT, но hevc.torrent_id > avc → НЕ overdue."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    avc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 5)
    # HEVC «старше» по ALTT created_at, но AniLibria id больше → актуальная пара.
    hevc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 48)
    rows = [
        _row(archive_id=1, torrent_id=100, codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=200, codec="HEVC", created_at=hevc_ts),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert unpaired == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_hevc_lower_torrent_id_overdue_after_sla() -> None:
    """hevc.torrent_id < avc.torrent_id и age>24h → overdue (staleness по id)."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    avc_ts = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    # HEVC «новее» по ALTT created_at — раньше это скрывало overdue.
    hevc_ts = now - timedelta(hours=1)
    rows = [
        _row(archive_id=1, torrent_id=300, codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=100, codec="HEVC", created_at=hevc_ts),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].missing is False
    assert unpaired[0].hevc_outdated is True
    assert unpaired[0].overdue is True
    assert unpaired[0].status == "overdue"
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == {1}
    assert release_ids_matching_hevc_filter(rows, hevc_filter="missing", now=now) == set()


def test_paired_hevc_then_avc_within_grace_not_overdue() -> None:
    """Grand Blue-like: HEVC tid+1 AVC через ~1ч — парная заливка, не overdue.

    Без grace меньший hevc.torrent_id + якорь со старого AVC 1-3 давали
    ложные «просрочка 288ч» при свежей exact-паре 1-4.
    """
    now = datetime(2026, 7, 29, 14, 0, 0)
    hevc_api = now - timedelta(hours=2)
    avc_api = now - timedelta(hours=1)
    old_avc_api = now - timedelta(hours=288 + HEVC_SLA_HOURS)
    rows = [
        _row(
            archive_id=3303,
            release_id=10241,
            torrent_id=39000,
            episodes="1-3",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=old_avc_api,
            api_created_at=old_avc_api,
            api_present=False,
            superseded=True,
        ),
        _row(
            archive_id=3469,
            release_id=10241,
            torrent_id=39245,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=hevc_api,
            api_created_at=hevc_api,
            info_hash="557b30427f11584b7ae94f10c0b163efd2dd376f",
        ),
        _row(
            archive_id=3470,
            release_id=10241,
            torrent_id=39246,
            episodes="1-4",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_api,
            api_created_at=avc_api,
            info_hash="96181d505c06e355bf14adb7354ba482197baa8d",
        ),
    ]
    assert find_unpaired_avc(rows, now=now) == []
    assert release_ids_matching_hevc_filter(rows, hevc_filter="overdue", now=now) == set()


def test_avc_reupload_after_grace_still_overdue() -> None:
    """AVC перезалит спустя >grace после exact-HEVC → catch-up/overdue."""
    now = datetime(2026, 7, 29, 14, 0, 0)
    hevc_api = now - timedelta(hours=48)
    avc_api = now - timedelta(hours=HEVC_SLA_HOURS + 2)
    rows = [
        _row(
            archive_id=1,
            torrent_id=39246,
            episodes="1-4",
            codec="AVC",
            rip_type="WEB-DL",
            created_at=avc_api,
            api_created_at=avc_api,
        ),
        _row(
            archive_id=2,
            torrent_id=39245,
            episodes="1-4",
            codec="HEVC",
            rip_type="WEB-DL",
            created_at=hevc_api,
            api_created_at=hevc_api,
        ),
    ]
    unpaired = find_unpaired_avc(rows, now=now)
    assert len(unpaired) == 1
    assert unpaired[0].hevc_outdated is True
    assert unpaired[0].overdue is True
    assert unpaired[0].status == "overdue"


def test_hevc_lower_torrent_id_not_overdue_within_sla() -> None:
    """Устаревший по torrent_id HEVC, но age≤24h → без бейджа overdue."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    avc_ts = now - timedelta(hours=3)
    hevc_ts = now - timedelta(hours=1)
    rows = [
        _row(archive_id=1, torrent_id=300, codec="AVC", created_at=avc_ts),
        _row(archive_id=2, torrent_id=100, codec="HEVC", created_at=hevc_ts),
    ]
    assert find_unpaired_avc(rows, now=now) == []


# --- 3: ignore_hevc ---


def test_ignore_hevc_clears_missing_and_overdue() -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 3)
    missing_rows = [
        _row(archive_id=1, torrent_id=10, codec="AVC", created_at=now, ignore_hevc=True),
    ]
    assert find_unpaired_avc(missing_rows, now=now) == []
    assert release_ids_matching_hevc_filter(missing_rows, hevc_filter="missing", now=now) == set()

    overdue_rows = [
        _row(archive_id=2, torrent_id=300, codec="AVC", created_at=old, ignore_hevc=True),
        _row(archive_id=3, torrent_id=100, codec="HEVC", created_at=old),
    ]
    assert find_unpaired_avc(overdue_rows, now=now) == []
    assert release_ids_matching_hevc_filter(overdue_rows, hevc_filter="overdue", now=now) == set()


def test_include_ignored_brings_back_ignore_hevc_avc() -> None:
    """include_ignored=True — ignored AVC снова в missing/overdue фильтрах."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=HEVC_SLA_HOURS + 3)
    missing_rows = [
        _row(archive_id=1, torrent_id=10, codec="AVC", created_at=now, ignore_hevc=True),
    ]
    assert find_unpaired_avc(missing_rows, now=now, include_ignored=True)
    assert release_ids_matching_hevc_filter(
        missing_rows, hevc_filter="missing", now=now, include_ignored=True
    ) == {1}

    overdue_rows = [
        _row(
            archive_id=2,
            torrent_id=300,
            codec="AVC",
            created_at=old,
            ignore_hevc=True,
            episodes="1-12",
        ),
        _row(
            archive_id=3,
            torrent_id=100,
            codec="HEVC",
            created_at=old,
            episodes="1-11",
        ),
    ]
    unpaired = find_unpaired_avc(overdue_rows, now=now, include_ignored=True)
    assert any(u.overdue for u in unpaired)
    assert release_ids_matching_hevc_filter(
        overdue_rows, hevc_filter="overdue", now=now, include_ignored=True
    ) == {1}


def test_ignore_hevc_resets_on_supersede_new_archive_row() -> None:
    """Новая версия (новый archive после supersede) стартует с ignore_hevc=False."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    # Старая superseded-строка с ignore — не участвует (require_active).
    old_ignored = _row(
        archive_id=1, torrent_id=10, codec="AVC", created_at=now, ignore_hevc=True
    )
    old_ignored.api_present = False
    old_ignored.superseded = True
    # Новая активная версия того же torrent_id — ignore сброшен.
    new_active = _row(
        archive_id=2, torrent_id=10, codec="AVC", created_at=now, ignore_hevc=False
    )
    unpaired = find_unpaired_avc([old_ignored, new_active], now=now)
    assert len(unpaired) == 1
    assert unpaired[0].archive_id == 2
    assert unpaired[0].missing is True


def test_toggle_ignore_hevc_endpoint_and_hevc_status(monkeypatch) -> None:
    """POST /releases/archive/{id}/ignore-hevc пишет флаг и дергает hevc_status sync."""
    from app.main import toggle_ignore_hevc

    archive = SimpleNamespace(
        id=42,
        release_id=7,
        torrent_id=99,
        torrent_type="BDRip 1080p AVC",
        torrent_description="1-12",
        quality_json=_qj(codec="AVC"),
        api_present=True,
        superseded=False,
        ignore_hevc=False,
        info_hash="ab" * 20,
        created_at=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
    )
    db = MagicMock()
    db.get.return_value = archive
    db.scalars.return_value.all.return_value = [archive]
    synced: list[int] = []

    monkeypatch.setattr(
        "app.main.sync_hevc_pair_events_for_release",
        lambda _db, rid: synced.append(rid) or 1,
    )
    monkeypatch.setattr(
        "app.main.templates.TemplateResponse",
        lambda request, name, ctx: SimpleNamespace(template=name, context=ctx),
    )

    request = MagicMock()
    out = toggle_ignore_hevc(request, archive_id=42, enabled="on", db=db)
    assert archive.ignore_hevc is True
    db.commit.assert_called()
    assert synced == [7]
    assert out.template == "partials/release_torrent_type_cell.html"
    assert out.context["t"].ignore_hevc is True
    assert out.context["t"].hevc_pair_status is None

    out2 = toggle_ignore_hevc(request, archive_id=42, enabled=None, db=db)
    assert archive.ignore_hevc is False
    assert out2.context["t"].ignore_hevc is False
    # Без HEVC-пары после снятия игнора — missing
    assert out2.context["t"].hevc_pair_status == "missing"


# --- 4: multi-torrent sibling ≠ removed ---


def test_multi_torrent_sibling_orphans_not_shown_as_removed(tmp_path, monkeypatch) -> None:
    """Bleach-like: файлы соседнего торрента в общей папке не «удалён» у нового."""
    from app.services.releases_view import _filter_removed_candidates

    media = tmp_path / "anilibria"
    show = media / "Bleach"
    show.mkdir(parents=True)
    ep346 = show / "ep346.mkv"
    ep347 = show / "ep347.mkv"
    ep346.write_bytes(b"346")
    ep347.write_bytes(b"347")
    true_orphan = show / "extra.mkv"
    true_orphan.write_bytes(b"x")

    monkeypatch.setattr("app.services.releases_view.resolve_media_root", lambda: media)
    monkeypatch.setattr(
        "app.services.releases_view.resolve_orphan_scan_root",
        lambda **kwargs: show.resolve(),
    )

    # Новый торрент 347-350: только ep347 в составе.
    files_new = [
        SimpleNamespace(relative_path="Bleach/ep347.mkv", full_path=str(ep347.resolve())),
    ]
    # Orphan-кандидаты: sibling ep346 + true orphan + sticky removed своего prior.
    candidates = [
        ("Bleach/ep346.mkv", str(ep346.resolve()), "orphan"),
        (str(true_orphan.resolve()), str(true_orphan.resolve()), "orphan"),
        ("Bleach/ep_old.mkv", str(show / "ep_old.mkv"), "removed"),
    ]
    sibling_rels = {"Bleach/ep346.mkv"}
    sibling_fulls = {str(ep346.resolve())}

    filtered = _filter_removed_candidates(
        candidates,
        files_new,  # type: ignore[arg-type]
        sibling_rel_paths=sibling_rels,
        sibling_full_paths=sibling_fulls,
    )
    displays = {d for d, _f, _k in filtered}
    assert "Bleach/ep346.mkv" not in displays  # sibling
    assert str(true_orphan.resolve()) in displays  # настоящий orphan
    assert "Bleach/ep_old.mkv" in displays  # sticky removed vs prior


def test_multi_torrent_sibling_filter_in_file_rows(tmp_path, monkeypatch) -> None:
    """_build_file_rows: sibling orphan не попадает в UI; sticky removed — да."""
    monkeypatch.setattr(
        "app.services.releases_view.file_status_for_ui",
        lambda **kwargs: "removed" if not kwargs.get("in_torrent", True) else "ok",
    )
    media = tmp_path / "show"
    media.mkdir()
    sib = str((media / "ep345.mkv").resolve())
    gone = str((media / "gone.mkv").resolve())

    files = [
        SimpleNamespace(
            id=1,
            relative_path="ep350.mkv",
            size=1,
            selected=True,
            full_path=str((media / "ep350.mkv").resolve()),
            ui_status="new",
        )
    ]
    # Сначала фильтруем как в list_release_groups.
    filtered = _filter_removed_candidates(
        [
            ("ep345.mkv", sib, "orphan"),
            ("gone.mkv", gone, "removed"),
        ],
        files,  # type: ignore[arg-type]
        sibling_rel_paths={"ep345.mkv"},
        sibling_full_paths={sib},
    )
    rows = _build_file_rows(files, {}, None, removed_candidates=filtered)
    by_path = {r.relative_path: r for r in rows}
    assert "ep350.mkv" in by_path
    assert "ep345.mkv" not in by_path
    assert by_path["gone.mkv"].status == "removed"
    assert by_path["gone.mkv"].in_torrent is False
