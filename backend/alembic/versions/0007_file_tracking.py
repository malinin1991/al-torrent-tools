"""api_present, torrent_files, disk_file_hashes, file_change_events."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_file_tracking"
down_revision: str | None = "0006_telegram_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_archive",
        sa.Column("api_present", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_torrent_archive_api_present", "torrent_archive", ["api_present"])

    op.create_table(
        "torrent_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("torrent_id", sa.Integer(), nullable=False),
        sa.Column("info_hash", sa.String(length=128), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("full_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("info_hash", "relative_path", name="uq_torrent_files_hash_path"),
    )
    op.create_index("ix_torrent_files_torrent_id", "torrent_files", ["torrent_id"])
    op.create_index("ix_torrent_files_info_hash", "torrent_files", ["info_hash"])
    op.create_index("ix_torrent_files_release_id", "torrent_files", ["release_id"])
    op.create_index("ix_torrent_files_full_path", "torrent_files", ["full_path"])

    op.create_table(
        "disk_file_hashes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.Float(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("hash_algo", sa.String(length=32), nullable=False, server_default="blake3"),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("last_hashed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_path", name="uq_disk_file_hashes_full_path"),
    )

    op.create_table(
        "file_change_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("torrent_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("full_path", sa.Text(), nullable=True),
        sa.Column("details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_file_change_events_release_id", "file_change_events", ["release_id"])
    op.create_index("ix_file_change_events_torrent_id", "file_change_events", ["torrent_id"])
    op.create_index("ix_file_change_events_kind", "file_change_events", ["kind"])
    op.create_index("ix_file_change_events_full_path", "file_change_events", ["full_path"])


def downgrade() -> None:
    op.drop_index("ix_file_change_events_full_path", table_name="file_change_events")
    op.drop_index("ix_file_change_events_kind", table_name="file_change_events")
    op.drop_index("ix_file_change_events_torrent_id", table_name="file_change_events")
    op.drop_index("ix_file_change_events_release_id", table_name="file_change_events")
    op.drop_table("file_change_events")
    op.drop_table("disk_file_hashes")
    op.drop_index("ix_torrent_files_full_path", table_name="torrent_files")
    op.drop_index("ix_torrent_files_release_id", table_name="torrent_files")
    op.drop_index("ix_torrent_files_info_hash", table_name="torrent_files")
    op.drop_index("ix_torrent_files_torrent_id", table_name="torrent_files")
    op.drop_table("torrent_files")
    op.drop_index("ix_torrent_archive_api_present", table_name="torrent_archive")
    op.drop_column("torrent_archive", "api_present")
