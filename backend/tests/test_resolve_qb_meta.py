from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.pipeline import TorrentPipelineService


def test_resolve_qb_meta_fallback_sibling_and_extra() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db, job_id=1)
    pipeline = SimpleNamespace(id=7, info_hash="a" * 40, torrent_id=11, release_id=99)

    archive = SimpleNamespace(
        anime_name="Title",
        torrent_description="1-2",
        torrent_type="WEBRip",
        quality_json={"names": {"english": "Orig"}},
        category="winter.2024",
        release_alias=None,
    )

    # 1) archive  2) sibling alias  3) sibling for genres  4) ExtraUrl
    db.scalar.side_effect = [archive, "sibling-alias", None, None]

    rename, comment, category, tags = service._resolve_qb_meta(pipeline)

    assert category == "winter.2024"
    assert rename is not None and "Title" in rename
    assert comment == "https://aniliberty.top/anime/releases/release/sibling-alias/torrents"
    assert tags == []


def test_resolve_qb_meta_without_archive_uses_extra_url() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db, job_id=1)
    pipeline = SimpleNamespace(id=8, info_hash="b" * 40, torrent_id=12, release_id=100)

    # archive, sibling alias, sibling genres, ExtraUrl
    db.scalar.side_effect = [None, None, None, "from-extra"]

    rename, comment, category, tags = service._resolve_qb_meta(pipeline)

    assert rename is None
    assert category is None
    assert tags == []
    assert comment == "https://aniliberty.top/anime/releases/release/from-extra/torrents"


def test_resolve_qb_meta_reads_genres_from_archive() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db, job_id=1)
    pipeline = SimpleNamespace(id=9, info_hash="c" * 40, torrent_id=13, release_id=101)

    archive = SimpleNamespace(
        anime_name="Title",
        torrent_description="1",
        torrent_type="WEBRip",
        quality_json={"genres": ["Комедия", "Повседневность"]},
        category="AniLibria/2022",
        release_alias="x",
    )
    db.scalar.side_effect = [archive, None, None]

    _rename, _comment, _category, tags = service._resolve_qb_meta(pipeline)

    assert tags == ["Комедия", "Повседневность"]
