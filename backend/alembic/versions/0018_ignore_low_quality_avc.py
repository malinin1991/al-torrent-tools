"""Игнор HEVC для исторических AVC ниже 1080p в релизах без HEVC."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018_ignore_low_quality_avc"
down_revision: str | None = "0017_telegram_hevc_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Классификация повторяет app.services.hevc_pairing:
# quality_json.codec/type/label имеют приоритет, torrent_type — fallback.
# Quality берётся из quality, иначе из 360p…8k внутри type/type_text
# (как runtime: «WEBRip 720p» без отдельного quality), иначе из torrent_type.
# В CTE намеренно нет фильтров api_present/superseded: HEVC в любой версии
# релиза запрещает автоматический ignore для всего release_id.
BACKFILL_SQL = r"""
WITH extracted AS (
    SELECT
        ta.id,
        ta.release_id,
        ta.ignore_hevc,
        ta.torrent_type,
        concat_ws(
            ' ',
            CASE
                WHEN jsonb_typeof(ta.quality_json -> 'codec') = 'object'
                    THEN concat_ws(
                        ' ',
                        ta.quality_json #>> '{codec,label}',
                        ta.quality_json #>> '{codec,value}',
                        ta.quality_json #>> '{codec,description}'
                    )
                WHEN jsonb_typeof(ta.quality_json -> 'codec') = 'string'
                    THEN ta.quality_json ->> 'codec'
            END,
            CASE
                WHEN jsonb_typeof(ta.quality_json -> 'label') = 'object'
                    THEN concat_ws(
                        ' ',
                        ta.quality_json #>> '{label,label}',
                        ta.quality_json #>> '{label,value}',
                        ta.quality_json #>> '{label,description}'
                    )
                WHEN jsonb_typeof(ta.quality_json -> 'label') = 'string'
                    THEN ta.quality_json ->> 'label'
            END,
            CASE
                WHEN jsonb_typeof(ta.quality_json -> 'type') = 'object'
                    THEN concat_ws(
                        ' ',
                        ta.quality_json #>> '{type,label}',
                        ta.quality_json #>> '{type,value}',
                        ta.quality_json #>> '{type,description}'
                    )
                WHEN jsonb_typeof(ta.quality_json -> 'type') = 'string'
                    THEN ta.quality_json ->> 'type'
            END
        ) AS quality_codec_blob,
        COALESCE(
            NULLIF(btrim(ta.quality_json #>> '{quality,value}'), ''),
            NULLIF(btrim(ta.quality_json #>> '{quality,description}'), ''),
            NULLIF(btrim(ta.quality_json #>> '{quality,label}'), ''),
            CASE
                WHEN jsonb_typeof(ta.quality_json -> 'quality') = 'string'
                    THEN NULLIF(btrim(ta.quality_json ->> 'quality'), '')
            END
        ) AS quality_text,
        COALESCE(
            NULLIF(btrim(ta.quality_json #>> '{type,value}'), ''),
            NULLIF(btrim(ta.quality_json #>> '{type,description}'), ''),
            NULLIF(btrim(ta.quality_json #>> '{type,label}'), ''),
            CASE
                WHEN jsonb_typeof(ta.quality_json -> 'type') = 'string'
                    THEN NULLIF(btrim(ta.quality_json ->> 'type'), '')
            END
        ) AS type_text
    FROM torrent_archive AS ta
),
classified AS (
    SELECT
        e.*,
        CASE
            WHEN lower(e.quality_codec_blob) LIKE '%av1%' THEN 'AV1'
            WHEN lower(e.quality_codec_blob) LIKE '%hevc%'
                OR lower(e.quality_codec_blob) LIKE '%x265%'
                OR lower(e.quality_codec_blob) LIKE '%h.265%'
                OR lower(e.quality_codec_blob) LIKE '%h265%'
                THEN 'HEVC'
            WHEN lower(e.quality_codec_blob) LIKE '%avc%'
                OR lower(e.quality_codec_blob) LIKE '%x264%'
                OR lower(e.quality_codec_blob) LIKE '%h.264%'
                OR lower(e.quality_codec_blob) LIKE '%h264%'
                THEN 'AVC'
            WHEN lower(COALESCE(e.torrent_type, '')) LIKE '%av1%' THEN 'AV1'
            WHEN lower(COALESCE(e.torrent_type, '')) LIKE '%hevc%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%x265%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%h.265%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%h265%'
                THEN 'HEVC'
            WHEN lower(COALESCE(e.torrent_type, '')) LIKE '%avc%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%x264%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%h.264%'
                OR lower(COALESCE(e.torrent_type, '')) LIKE '%h264%'
                THEN 'AVC'
        END AS codec,
        lower(
            CASE
                WHEN e.type_text IS NOT NULL OR e.quality_text IS NOT NULL
                    THEN COALESCE(
                        NULLIF(btrim(e.quality_text), ''),
                        (
                            regexp_match(
                                COALESCE(e.type_text, ''),
                                '(^|[[:space:]])(360p|480p|576p|720p|1080p|2k|4k|8k)([[:space:]]|$)',
                                'i'
                            )
                        )[2],
                        ''
                    )
                ELSE COALESCE(
                    (regexp_match(
                        COALESCE(e.torrent_type, ''),
                        '(^|[[:space:]])(360p|480p|576p|720p|1080p|2k|4k|8k)([[:space:]]|$)',
                        'i'
                    ))[2],
                    ''
                )
            END
        ) AS quality
    FROM extracted AS e
)
UPDATE torrent_archive AS target
SET ignore_hevc = true
FROM classified AS candidate
WHERE target.id = candidate.id
  AND target.ignore_hevc IS false
  AND candidate.codec = 'AVC'
  AND candidate.quality IN ('360p', '480p', '576p', '720p')
  AND NOT EXISTS (
      SELECT 1
      FROM classified AS sibling
      WHERE sibling.release_id = candidate.release_id
        AND sibling.codec = 'HEVC'
  )
"""


def upgrade() -> None:
    op.execute(sa.text(BACKFILL_SQL))


def downgrade() -> None:
    # No-op намеренно: без отдельного служебного маркера нельзя отличить флаги,
    # выставленные этой миграцией, от пользовательских ignore_hevc=true.
    # Сбрасывать пользовательские значения при downgrade небезопасно.
    pass
