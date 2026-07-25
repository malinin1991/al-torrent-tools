"""Таблица pipeline_events — audit trail пайплайна."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_pipeline_events"
down_revision: str | None = "0011_torrent_history_per_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["torrent_pipeline.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_events_pipeline_id", "pipeline_events", ["pipeline_id"])
    op.create_index("ix_pipeline_events_job_id", "pipeline_events", ["job_id"])
    op.create_index("ix_pipeline_events_event_type", "pipeline_events", ["event_type"])
    op.create_index("ix_pipeline_events_pipeline_id_id", "pipeline_events", ["pipeline_id", "id"])
    op.create_index("ix_pipeline_events_created_at", "pipeline_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_created_at", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_pipeline_id_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_event_type", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_job_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_pipeline_id", table_name="pipeline_events")
    op.drop_table("pipeline_events")
