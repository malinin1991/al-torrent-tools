"""Расширение torrent_type до 128 символов (WEBRip 1080p HEVC)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_torrent_type_len"
down_revision: str | None = "0003_archive_file_size_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "torrent_archive",
        "torrent_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "torrent_archive",
        "torrent_type",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
