from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.releases_view import format_bytes, list_release_groups


def test_format_bytes() -> None:
    assert format_bytes(None) == "-"
    assert format_bytes(500) == "500 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_list_release_groups_exposes_genres() -> None:
    db = MagicMock()
    stats_row = SimpleNamespace(release_id=10, last_updated=None, torrent_count=1)
    db.scalar.return_value = 1
    db.execute.return_value.all.return_value = [stats_row]
    archive = SimpleNamespace(
        id=1,
        release_id=10,
        release_alias="test-show",
        anime_name="Тест",
        category="AniLibria/2024",
        torrent_id=100,
        info_hash="a" * 40,
        torrent_type="WEBRip",
        torrent_description="1-12",
        file_size=1024,
        created_at=None,
        quality_json={"genres": ["Комедия", "Романтика"]},
    )
    db.scalars.side_effect = [
        MagicMock(all=lambda: [archive]),  # archives
        MagicMock(all=lambda: []),  # pipelines
    ]

    result = list_release_groups(db, page=1, per_page=30)

    assert len(result["groups"]) == 1
    assert result["groups"][0].genres == ["Комедия", "Романтика"]
