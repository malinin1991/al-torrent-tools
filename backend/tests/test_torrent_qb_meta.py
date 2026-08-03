from app.services.qbittorrent import ensure_announce_passkey
from app.services.torrent_qb_meta import (
    build_qb_torrent_name,
    build_qb_torrent_name_from_payloads,
    build_release_torrents_url,
    first_release_torrents_url,
)


def test_build_qb_torrent_name_full() -> None:
    assert (
        build_qb_torrent_name(
            main_name="Блич",
            original_name="Bleach",
            episodes="1-189",
            torrent_type="BDRip 1080p AVC",
        )
        == "Блич / Bleach (1-189) [BDRip 1080p AVC]"
    )


def test_build_qb_torrent_name_from_payloads() -> None:
    name = build_qb_torrent_name_from_payloads(
        {"name": {"main": "Мао", "english": "Mao"}},
        {
            "description": "1-12",
            "type": {"value": "WEBRip"},
            "quality": {"value": "1080p"},
            "codec": {"label": "HEVC"},
        },
    )
    assert name == "Мао / Mao (1-12) [WEBRip 1080p HEVC]"


def test_build_release_torrents_url() -> None:
    url = build_release_torrents_url("lets-go-kaiki-gumi", site_url="https://www.anilibria.top")
    assert url == "https://www.anilibria.top/anime/releases/release/lets-go-kaiki-gumi/torrents"


def test_build_release_admin_url() -> None:
    from app.services.torrent_qb_meta import build_release_admin_url

    tpl = "https://adminka.example/anime/release/{release_id}#tab=torrents"
    assert (
        build_release_admin_url(10232, tpl)
        == "https://adminka.example/anime/release/10232#tab=torrents"
    )
    assert build_release_admin_url(1, "") is None
    assert build_release_admin_url(1, "   ") is None
    assert build_release_admin_url(1, None) is None
    assert build_release_admin_url(1, "https://adminka.example/no-placeholder") is None


def test_first_release_torrents_url_skips_empty() -> None:
    url = first_release_torrents_url(
        None,
        "",
        "  ",
        "lets-go-kaiki-gumi",
        site_url="https://www.anilibria.top",
    )
    assert url == "https://www.anilibria.top/anime/releases/release/lets-go-kaiki-gumi/torrents"


def test_extract_release_genres() -> None:
    from app.services.torrent_qb_meta import extract_release_genres, genres_from_quality_json

    names = extract_release_genres(
        {
            "genres": [
                {"id": 1, "name": "Комедия"},
                {"id": 10, "name": "Повседневность"},
                {"id": 1, "name": "комедия"},
            ]
        }
    )
    assert names == ["Комедия", "Повседневность"]
    assert genres_from_quality_json({"genres": names}) == names


def test_extract_release_members_and_block_flags() -> None:
    from app.services.torrent_qb_meta import (
        block_flags_from_quality_json,
        extract_release_block_flags,
        extract_release_members,
        members_from_quality_json,
    )

    members = extract_release_members(
        {
            "members": [
                {
                    "nickname": "Zvukar",
                    "role": {"value": "voicing", "description": "Озвучка"},
                },
                {
                    "nickname": "Timer",
                    "role": {"value": "timing", "description": "Тайминг"},
                },
                {
                    "nickname": "Zvukar",
                    "role": {"value": "voicing", "description": "Озвучка"},
                },
                {"nickname": "Ghost", "role": {"value": "weird", "description": "?"}},
            ]
        }
    )
    assert members == [
        {"role": "voicing", "role_label": "Озвучка", "nickname": "Zvukar"},
        {"role": "timing", "role_label": "Тайминг", "nickname": "Timer"},
        {"role": "unknown", "role_label": "?", "nickname": "Ghost"},
    ]
    assert members_from_quality_json({"members": members}) == members

    assert extract_release_block_flags(
        {"is_blocked_by_geo": True, "is_blocked_by_copyrights": False}
    ) == (True, False)
    assert extract_release_block_flags(
        {"is_blocked_by_geo": "true", "is_blocked_by_copyrights": "0"}
    ) == (True, False)
    assert block_flags_from_quality_json(
        {"is_blocked_by_geo": True, "is_blocked_by_copyrights": True}
    ) == (True, True)
    assert block_flags_from_quality_json(None) == (False, False)


def test_ensure_announce_passkey_injects_pk() -> None:
    announce = b"http://tr.libria.fun:2710/announce"
    info = b"d4:name4:test6:lengthi1ee"
    torrent = b"d8:announce" + f"{len(announce)}:".encode() + announce + b"4:info" + info + b"e"
    patched = ensure_announce_passkey(torrent, "XqvV10S2tvF5E4j2")
    assert b"?pk=XqvV10S2tvF5E4j2" in patched
    # info_hash не меняется
    from app.services.qbittorrent import torrent_info_hash

    assert torrent_info_hash(torrent) == torrent_info_hash(patched)


def test_ensure_announce_passkey_noop_without_key() -> None:
    raw = b"d8:announce34:http://tr.libria.fun:2710/announce4:infod4:name4:teste"
    assert ensure_announce_passkey(raw, "") is raw
