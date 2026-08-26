"""Временный overlay is_checking на torrent_files (не sticky ui_status)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0019_torrent_file_is_checking"
down_revision: str | None = "0018_ignore_low_quality_avc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_files",
        sa.Column(
            "is_checking",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("torrent_files", "is_checking")
