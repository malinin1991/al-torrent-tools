"""Таблицы tracked_releases, telegram_outbox и колонка tg_status на pipeline."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_telegram_tracking"
down_revision: str | None = "0005_release_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tracked_releases",
        sa.Column("release_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("release_alias", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="ui"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("release_id"),
    )
    op.create_index("ix_tracked_releases_release_alias", "tracked_releases", ["release_alias"])
    op.create_index("ix_tracked_releases_enabled", "tracked_releases", ["enabled"])

    op.create_table(
        "telegram_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["pipeline_id"], ["torrent_pipeline.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_telegram_outbox_status", "telegram_outbox", ["status"])
    op.create_index("ix_telegram_outbox_pipeline_id", "telegram_outbox", ["pipeline_id"])

    op.add_column(
        "torrent_pipeline",
        sa.Column("tg_status", sa.String(length=16), nullable=False, server_default="skipped"),
    )


def downgrade() -> None:
    op.drop_column("torrent_pipeline", "tg_status")
    op.drop_index("ix_telegram_outbox_pipeline_id", table_name="telegram_outbox")
    op.drop_index("ix_telegram_outbox_status", table_name="telegram_outbox")
    op.drop_table("telegram_outbox")
    op.drop_index("ix_tracked_releases_enabled", table_name="tracked_releases")
    op.drop_index("ix_tracked_releases_release_alias", table_name="tracked_releases")
    op.drop_table("tracked_releases")
