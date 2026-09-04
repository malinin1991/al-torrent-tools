"""Operational overlay media_present на torrent_files (не sticky ui_status)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0021_torrent_file_media_present"
down_revision: str | None = "0020_file_mediainfo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_files",
        sa.Column(
            "media_present",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("torrent_files", "media_present")
