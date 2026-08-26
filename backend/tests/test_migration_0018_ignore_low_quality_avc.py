"""PostgreSQL-проверка data migration 0018.

Для запуска нужен отдельный URL тестовой БД:
ALTT_TEST_POSTGRES_URL=postgresql+psycopg://... pytest -q \
  tests/test_migration_0018_ignore_low_quality_avc.py
"""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def _backfill_sql() -> str:
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0018_ignore_low_quality_avc.py"
    )
    return str(runpy.run_path(str(migration))["BACKFILL_SQL"])


def test_backfill_sql_extracts_quality_from_type_when_quality_missing() -> None:
    sql = _backfill_sql()
    assert "COALESCE(e.type_text, '')" in sql
    assert "(360p|480p|576p|720p|1080p|2k|4k|8k)" in sql
    type_branch, torrent_branch = sql.split("ELSE COALESCE", 1)
    assert "regexp_match" in type_branch
    assert "regexp_match" in torrent_branch


def test_backfill_low_quality_avc_without_hevc_on_postgresql() -> None:
    url = os.getenv("ALTT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ALTT_TEST_POSTGRES_URL не задан")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("ALTT_TEST_POSTGRES_URL должен указывать на PostgreSQL")

    engine = create_engine(url)
    rows = [
        # Меняются: quality_json object/string и безопасный fallback torrent_type.
        (1, 101, "BDRip 720p AVC", {"quality": {"value": "720p"}, "codec": {"label": "AVC"}}, False),
        (2, 102, "WEBRip 576p x264", {"quality": "576p", "codec": "x264/AVC"}, False),
        (3, 103, "BDRip 480p AVC", {}, False),
        # Не меняются: 1080p, unknown, AV1 и сам HEVC.
        (4, 104, "BDRip 1080p AVC", {"quality": {"value": "1080p"}, "codec": {"label": "AVC"}}, False),
        (5, 105, "BDRip HD AVC", {"quality": {"value": "HD"}, "codec": {"label": "AVC"}}, False),
        (6, 106, "WEBRip 720p AVC", {"quality": {"value": "720p"}, "codec": {"label": "AV1"}}, False),
        (7, 107, "WEBRip 720p HEVC", {"quality": {"value": "720p"}, "codec": {"label": "HEVC"}}, False),
        # Исторический HEVC (api_present=false, superseded=true) блокирует весь release.
        (8, 108, "WEBRip 720p AVC", {"quality": {"value": "720p"}, "codec": {"label": "AVC"}}, False),
        (9, 108, "WEBRip 720p HEVC", {"quality": {"value": "720p"}, "codec": {"label": "HEVC"}}, False),
        # Уже выставленный пользовательский флаг остаётся true.
        (10, 109, "BDRip 1080p AVC", {"quality": {"value": "1080p"}, "codec": {"label": "AVC"}}, True),
        # Quality только внутри type / type_text — как runtime _type_and_quality_parts.
        (11, 110, "", {"type": {"label": "WEBRip 720p"}, "codec": {"label": "AVC"}}, False),
        (12, 111, "unused label", {"type": "BDRip 480p", "codec": "AVC"}, False),
        (13, 112, "", {"type": {"value": "WEBRip 360p"}, "codec": {"label": "AVC"}}, False),
        (14, 113, "", {"type": {"label": "WEBRip 576p"}, "codec": {"label": "AVC"}}, False),
        # 1080p в type не ignore; HEVC-sibling блокирует type-only 720p.
        (15, 114, "", {"type": {"label": "WEBRip 1080p"}, "codec": {"label": "AVC"}}, False),
        (16, 115, "", {"type": {"label": "WEBRip 720p"}, "codec": {"label": "AVC"}}, False),
        (17, 115, "", {"type": {"label": "WEBRip 1080p"}, "codec": {"label": "HEVC"}}, False),
    ]

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TEMPORARY TABLE torrent_archive (
                    id integer PRIMARY KEY,
                    release_id integer NOT NULL,
                    torrent_type varchar(128),
                    quality_json jsonb NOT NULL,
                    ignore_hevc boolean NOT NULL,
                    api_present boolean NOT NULL DEFAULT true,
                    superseded boolean NOT NULL DEFAULT false
                ) ON COMMIT DROP
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO torrent_archive (
                    id, release_id, torrent_type, quality_json, ignore_hevc
                ) VALUES (
                    :id, :release_id, :torrent_type, CAST(:quality_json AS jsonb), :ignore_hevc
                )
                """
            ),
            [
                {
                    "id": row[0],
                    "release_id": row[1],
                    "torrent_type": row[2],
                    "quality_json": json.dumps(row[3]),
                    "ignore_hevc": row[4],
                }
                for row in rows
            ],
        )
        connection.execute(
            text(
                """
                UPDATE torrent_archive
                SET api_present = false, superseded = true
                WHERE id = 9
                """
            )
        )

        connection.execute(text(_backfill_sql()))
        actual = dict(
            connection.execute(
                text("SELECT id, ignore_hevc FROM torrent_archive ORDER BY id")
            ).all()
        )

        assert actual == {
            1: True,
            2: True,
            3: True,
            4: False,
            5: False,
            6: False,
            7: False,
            8: False,
            9: False,
            10: True,
            11: True,
            12: True,
            13: True,
            14: True,
            15: False,
            16: False,
            17: False,
        }

        # Повторный upgrade идемпотентен.
        connection.execute(text(_backfill_sql()))
        repeated = dict(
            connection.execute(
                text("SELECT id, ignore_hevc FROM torrent_archive ORDER BY id")
            ).all()
        )
        assert repeated == actual
