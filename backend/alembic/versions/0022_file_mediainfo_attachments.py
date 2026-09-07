"""Колонки вложений в file_mediainfo."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0022_file_mediainfo_attachments"
down_revision: str | None = "0021_torrent_file_media_present"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_mediainfo",
        sa.Column(
            "attachments_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "file_mediainfo",
        sa.Column(
            "attachments_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "file_mediainfo",
        sa.Column("attachments_total_bytes", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_mediainfo", "attachments_total_bytes")
    op.drop_column("file_mediainfo", "attachments_count")
    op.drop_column("file_mediainfo", "attachments_json")
