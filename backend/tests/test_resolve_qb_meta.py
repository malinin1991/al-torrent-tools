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

    # 1) archive by hash/id  2) sibling alias  3) ExtraUrl (unused here)
    db.scalar.side_effect = [archive, "sibling-alias", None]

    rename, comment, category = service._resolve_qb_meta(pipeline)

    assert category == "winter.2024"
    assert rename is not None and "Title" in rename
    assert comment == "https://www.anilibria.top/anime/releases/release/sibling-alias/torrents"


def test_resolve_qb_meta_without_archive_uses_extra_url() -> None:
    db = MagicMock()
    service = TorrentPipelineService(db, job_id=1)
    pipeline = SimpleNamespace(id=8, info_hash="b" * 40, torrent_id=12, release_id=100)

    # archive missing, sibling missing, ExtraUrl present
    db.scalar.side_effect = [None, None, "from-extra"]

    rename, comment, category = service._resolve_qb_meta(pipeline)

    assert rename is None
    assert category is None
    assert comment == "https://www.anilibria.top/anime/releases/release/from-extra/torrents"
