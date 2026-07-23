"""Sticky ui_status на torrent_files."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010_torrent_file_ui_status"
down_revision: str | None = "0009_purge_false_orphan_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_files",
        sa.Column(
            "ui_status",
            sa.String(length=16),
            nullable=False,
            server_default="ok",
        ),
    )
    op.create_index("ix_torrent_files_ui_status", "torrent_files", ["ui_status"])


def downgrade() -> None:
    op.drop_index("ix_torrent_files_ui_status", table_name="torrent_files")
    op.drop_column("torrent_files", "ui_status")
