"""Постоянный список донаблюдения ongoing."""

from alembic import op
import sqlalchemy as sa

revision = "0023_ongoing_watch"
down_revision = "0022_file_mediainfo_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ongoing_watch",
        sa.Column("release_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("release_alias", sa.String(255), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("missing_since", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("max_torrent_id", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ongoing_watch_expires_at", "ongoing_watch", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ongoing_watch_expires_at", table_name="ongoing_watch")
    op.drop_table("ongoing_watch")
