"""Начальная миграция схемы."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "extra_urls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_alias", sa.String(length=255), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extra_urls_release_alias", "extra_urls", ["release_alias"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("params_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)
    op.create_index("ix_jobs_type", "jobs", ["type"], unique=False)
    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_logs_job_id", "job_logs", ["job_id"], unique=False)
    op.create_table(
        "seen_torrents",
        sa.Column("torrent_id", sa.Integer(), nullable=False),
        sa.Column("info_hash", sa.String(length=128), nullable=True),
        sa.Column("release_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("torrent_id"),
        sa.UniqueConstraint("info_hash", name="uq_seen_torrents_info_hash"),
    )
    op.create_index("ix_seen_torrents_release_id", "seen_torrents", ["release_id"], unique=False)
    op.create_table(
        "torrent_pipeline",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("info_hash", sa.String(length=128), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("torrent_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("master_added_at", sa.DateTime(), nullable=True),
        sa.Column("slave_added_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_torrent_pipeline_info_hash", "torrent_pipeline", ["info_hash"], unique=False)
    op.create_index("ix_torrent_pipeline_release_id", "torrent_pipeline", ["release_id"], unique=False)
    op.create_index("ix_torrent_pipeline_torrent_id", "torrent_pipeline", ["torrent_id"], unique=False)
    op.create_table(
        "torrent_archive",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("info_hash", sa.String(length=128), nullable=False),
        sa.Column("torrent_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("release_alias", sa.String(length=255), nullable=True),
        sa.Column("anime_name", sa.String(length=512), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quality_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_torrent_archive_info_hash", "torrent_archive", ["info_hash"], unique=False)
    op.create_index("ix_torrent_archive_release_alias", "torrent_archive", ["release_alias"], unique=False)
    op.create_index("ix_torrent_archive_release_id", "torrent_archive", ["release_id"], unique=False)
    op.create_index("ix_torrent_archive_torrent_id", "torrent_archive", ["torrent_id"], unique=False)
    op.create_table(
        "cleanup_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tracker_host", sa.String(length=255), nullable=False),
        sa.Column("message_contains", sa.String(length=255), nullable=False),
        sa.Column("include_errored", sa.Boolean(), nullable=False),
        sa.Column("delete_files", sa.Boolean(), nullable=False),
        sa.Column("target_client", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "qb_clients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qb_clients_role", "qb_clients", ["role"], unique=False)

    op.bulk_insert(
        sa.table(
            "cleanup_rules",
            sa.column("name", sa.String()),
            sa.column("tracker_host", sa.String()),
            sa.column("message_contains", sa.String()),
            sa.column("include_errored", sa.Boolean()),
            sa.column("delete_files", sa.Boolean()),
            sa.column("target_client", sa.String()),
            sa.column("enabled", sa.Boolean()),
        ),
        [
            {
                "name": "Либрия: не зарегистрирован",
                "tracker_host": "tr.libria.fun",
                "message_contains": "Торрент не зарегистрирован",
                "include_errored": True,
                "delete_files": False,
                "target_client": "master",
                "enabled": True,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_qb_clients_role", table_name="qb_clients")
    op.drop_table("qb_clients")
    op.drop_table("cleanup_rules")
    op.drop_index("ix_torrent_archive_torrent_id", table_name="torrent_archive")
    op.drop_index("ix_torrent_archive_release_id", table_name="torrent_archive")
    op.drop_index("ix_torrent_archive_release_alias", table_name="torrent_archive")
    op.drop_index("ix_torrent_archive_info_hash", table_name="torrent_archive")
    op.drop_table("torrent_archive")
    op.drop_index("ix_torrent_pipeline_torrent_id", table_name="torrent_pipeline")
    op.drop_index("ix_torrent_pipeline_release_id", table_name="torrent_pipeline")
    op.drop_index("ix_torrent_pipeline_info_hash", table_name="torrent_pipeline")
    op.drop_table("torrent_pipeline")
    op.drop_index("ix_seen_torrents_release_id", table_name="seen_torrents")
    op.drop_table("seen_torrents")
    op.drop_index("ix_job_logs_job_id", table_name="job_logs")
    op.drop_table("job_logs")
    op.drop_index("ix_jobs_type", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_extra_urls_release_alias", table_name="extra_urls")
    op.drop_table("extra_urls")
    op.drop_table("settings")
