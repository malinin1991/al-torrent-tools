"""Таблица file_mediainfo для кэширования MediaInfo файлов."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020_file_mediainfo"
down_revision: str | None = "0019_torrent_file_is_checking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_mediainfo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("mtime", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "raw_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_file_mediainfo_full_path",
        "file_mediainfo",
        ["full_path"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_file_mediainfo_full_path", table_name="file_mediainfo")
    op.drop_table("file_mediainfo")
