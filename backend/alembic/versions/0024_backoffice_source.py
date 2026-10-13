"""Источники релизов, ручной статус и политика админских торрентов."""

from alembic import op
import sqlalchemy as sa

revision = "0024_backoffice_source"
down_revision = "0023_ongoing_watch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("source", sa.String(16), nullable=True))
    op.add_column("releases", sa.Column("source_checked_at", sa.DateTime(), nullable=True))
    op.add_column("releases", sa.Column("is_hidden", sa.Boolean(), nullable=True))
    op.add_column("releases", sa.Column("is_abandoned", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_releases_source", "releases", ["source"])
    op.add_column("torrent_archive", sa.Column("backoffice_trackers", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("torrent_archive", sa.Column("notification_baseline", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("torrent_archive", "notification_baseline")
    op.drop_column("torrent_archive", "backoffice_trackers")
    op.drop_index("ix_releases_source", table_name="releases")
    for name in ("is_abandoned", "is_hidden", "source_checked_at", "source"):
        op.drop_column("releases", name)
