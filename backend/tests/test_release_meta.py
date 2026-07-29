"""Тесты карточки релиза: releases / release_members."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import Release, ReleaseMember
from app.services.release_meta import load_release_meta_by_ids, upsert_release_meta
from app.services.releases_view import list_release_groups


def _payload(**overrides):
    base = {
        "id": 42,
        "alias": "spy-x-family-3",
        "name": {"main": "Семья шпиона 3", "english": "Spy x Family S3"},
        "genres": [{"name": "Экшен"}, {"name": "Комедия"}],
        "members": [
            {
                "id": "uuid-1",
                "nickname": "Zvukar",
                "role": {"value": "voicing", "description": "Озвучка"},
            },
            {
                "id": "uuid-2",
                "nickname": "Timer",
                "role": {"value": "timing", "description": "Тайминг"},
            },
        ],
        "is_blocked_by_geo": True,
        "is_blocked_by_copyrights": False,
    }
    base.update(overrides)
    return base


def test_upsert_release_meta_creates_row_and_members() -> None:
    db = MagicMock()
    db.get.return_value = None
    stored: list = []

    def add(obj):
        stored.append(obj)

    db.add.side_effect = add
    row = upsert_release_meta(db, 42, _payload(), commit=True)
    assert row.release_id == 42
    assert row.release_alias == "spy-x-family-3"
    assert row.title == "Семья шпиона 3"
    assert row.genres_json == ["Экшен", "Комедия"]
    assert row.is_blocked_by_geo is True
    assert row.is_blocked_by_copyrights is False
    members = [o for o in stored if isinstance(o, ReleaseMember)]
    assert [(m.role, m.nickname, m.api_member_id) for m in members] == [
        ("voicing", "Zvukar", "uuid-1"),
        ("timing", "Timer", "uuid-2"),
    ]
    db.commit.assert_called()


def test_upsert_sparse_payload_does_not_clear_blocks() -> None:
    existing = Release(
        release_id=42,
        release_alias="old",
        title="Old",
        genres_json=["A"],
        is_blocked_by_geo=True,
        is_blocked_by_copyrights=True,
        updated_at=datetime(2026, 1, 1),
    )
    db = MagicMock()
    db.get.return_value = existing
    upsert_release_meta(
        db,
        42,
        {"id": 42, "alias": "new-alias", "genres": [{"name": "B"}]},
        commit=False,
    )
    assert existing.release_alias == "new-alias"
    assert existing.genres_json == ["B"]
    assert existing.is_blocked_by_geo is True
    assert existing.is_blocked_by_copyrights is True
    db.execute.assert_not_called()  # members key отсутствует


def test_upsert_empty_members_clears_composition() -> None:
    existing = Release(release_id=7, updated_at=datetime(2026, 1, 1))
    db = MagicMock()
    db.get.return_value = existing
    upsert_release_meta(db, 7, {"id": 7, "members": []}, commit=False)
    db.execute.assert_called_once()


def test_upsert_null_members_does_not_wipe() -> None:
    existing = Release(release_id=7, updated_at=datetime(2026, 1, 1))
    db = MagicMock()
    db.get.return_value = existing
    upsert_release_meta(db, 7, {"id": 7, "members": None}, commit=False)
    db.execute.assert_not_called()


def test_load_release_meta_by_ids_maps_members() -> None:
    release = Release(
        release_id=5,
        release_alias="a",
        title="T",
        genres_json=["G"],
        is_blocked_by_geo=False,
        is_blocked_by_copyrights=True,
        updated_at=datetime(2026, 1, 1),
        members=[
            ReleaseMember(
                id=1,
                release_id=5,
                role="voicing",
                role_label="Озвучка",
                nickname="A",
                sort_order=1,
                api_member_id="x",
            ),
            ReleaseMember(
                id=2,
                release_id=5,
                role="editing",
                role_label="Сведение",
                nickname="B",
                sort_order=0,
            ),
        ],
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [release]
    by_id = load_release_meta_by_ids(db, [5])
    assert set(by_id) == {5}
    view = by_id[5]
    assert view.genres == ["G"]
    assert view.is_blocked_by_copyrights is True
    assert [m["nickname"] for m in view.members] == ["B", "A"]  # sort_order


def test_list_release_groups_block_fallback_when_meta_null(monkeypatch) -> None:
    """releases.is_blocked_* = NULL → бейджи из quality_json."""
    from app.services import releases_view as rv
    from app.services.release_meta import ReleaseMetaView

    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    archive = SimpleNamespace(
        id=1,
        release_id=100,
        release_alias="alias",
        anime_name="Show",
        category="TV",
        created_at=now,
        torrent_id=1,
        info_hash="aa" * 20,
        torrent_type="WEB-DL 1080p AVC",
        torrent_description="1-12",
        file_size=100,
        api_present=True,
        superseded=False,
        ignore_hevc=False,
        api_created_at=None,
        quality_json={
            "genres": ["G"],
            "is_blocked_by_geo": True,
            "is_blocked_by_copyrights": True,
        },
    )
    stats = SimpleNamespace(release_id=100, last_updated=now, torrent_count=1)
    db = MagicMock()
    db.execute.return_value.all.return_value = [stats]
    db.scalar.return_value = 1
    db.scalars.return_value.all.return_value = [archive]

    monkeypatch.setattr(
        rv,
        "load_release_meta_by_ids",
        lambda _db, _ids: {
            100: ReleaseMetaView(
                release_id=100,
                genres=["G"],
                members=[],
                is_blocked_by_geo=None,
                is_blocked_by_copyrights=None,
            )
        },
    )
    monkeypatch.setattr(rv, "_latest_pipeline_by_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_tracked_by_release_id", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_files_by_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_recent_events_by_info_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_disk_hashes_by_path", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_info_hashes_with_active_hash_job", lambda *_a, **_k: set())
    monkeypatch.setattr(rv, "_active_file_paths_by_release", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "resolve_anilibria_site_url", lambda: "https://anilibria.tv")
    monkeypatch.setattr(rv, "unpaired_by_archive_id", lambda *_a, **_k: {})

    group = list_release_groups(db, page=1, per_page=30)["groups"][0]
    assert group.is_blocked_by_geo is True
    assert group.is_blocked_by_copyrights is True

    from app.services import releases_view as rv
    from app.services.release_meta import ReleaseMetaView

    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    archive = SimpleNamespace(
        id=1,
        release_id=100,
        release_alias="alias",
        anime_name="From Archive",
        category="TV",
        created_at=now,
        torrent_id=1,
        info_hash="aa" * 20,
        torrent_type="WEB-DL 1080p AVC",
        torrent_description="1-12",
        file_size=100,
        api_present=True,
        superseded=False,
        ignore_hevc=False,
        api_created_at=None,
        quality_json={
            "genres": ["Wrong"],
            "members": [{"role": "voicing", "role_label": "Озвучка", "nickname": "Old"}],
            "is_blocked_by_geo": False,
            "is_blocked_by_copyrights": False,
        },
    )
    stats = SimpleNamespace(release_id=100, last_updated=now, torrent_count=1)
    db = MagicMock()
    db.execute.return_value.all.return_value = [stats]
    db.scalar.return_value = 1
    db.scalars.return_value.all.return_value = [archive]

    monkeypatch.setattr(
        rv,
        "load_release_meta_by_ids",
        lambda _db, _ids: {
            100: ReleaseMetaView(
                release_id=100,
                genres=["Correct"],
                members=[
                    {
                        "role": "voicing",
                        "role_label": "Озвучка",
                        "nickname": "NewVoice",
                    }
                ],
                is_blocked_by_geo=True,
                is_blocked_by_copyrights=True,
            )
        },
    )
    monkeypatch.setattr(rv, "_latest_pipeline_by_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_tracked_by_release_id", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_files_by_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_recent_events_by_info_hash", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_disk_hashes_by_path", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "_info_hashes_with_active_hash_job", lambda *_a, **_k: set())
    monkeypatch.setattr(rv, "_active_file_paths_by_release", lambda *_a, **_k: {})
    monkeypatch.setattr(rv, "resolve_anilibria_site_url", lambda: "https://anilibria.tv")
    monkeypatch.setattr(rv, "unpaired_by_archive_id", lambda *_a, **_k: {})

    result = list_release_groups(db, page=1, per_page=30)
    group = result["groups"][0]
    assert group.genres == ["Correct"]
    assert group.members[0]["nickname"] == "NewVoice"
    assert group.is_blocked_by_geo is True
    assert group.is_blocked_by_copyrights is True
