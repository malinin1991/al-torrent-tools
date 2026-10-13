"""Structured synchronization outcomes and fenced Telegram delivery."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_job_outcomes_outbox"
down_revision = "0025_encoder_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("result_json", postgresql.JSONB(), nullable=True))
    op.add_column("jobs", sa.Column("result_updated_at", sa.DateTime(), nullable=True))
    op.create_table(
        "job_sync_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("release_id", sa.Integer()),
        sa.Column("torrent_id", sa.Integer()),
        sa.Column("info_hash", sa.String(128)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("operation", sa.String(48), nullable=False),
        sa.Column("qb_role", sa.String(16)),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_params", postgresql.JSONB(), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.UniqueConstraint("job_id", "object_key", name="uq_job_sync_failure_object"),
    )
    op.create_index("ix_job_sync_failures_job_id_id", "job_sync_failures", ["job_id", "id"])
    op.create_index("ix_job_sync_failures_retryable", "job_sync_failures", ["job_id", "retryable", "id"])
    for column in (
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("last_attempt_at", sa.DateTime()),
        sa.Column("last_error_kind", sa.String(32)),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("telegram_message_id", sa.BigInteger()),
    ):
        op.add_column("telegram_outbox", column)
    op.execute("UPDATE telegram_outbox SET updated_at = COALESCE(sent_at, created_at, timezone('UTC', now()))")
    op.alter_column("telegram_outbox", "updated_at", nullable=False,
                    server_default=sa.text("timezone('UTC', now())"))
    op.create_index("ix_telegram_outbox_due", "telegram_outbox", ["bot_key", "status", "next_attempt_at", "id"])
    op.create_index("ix_telegram_outbox_lease", "telegram_outbox", ["bot_key", "status", "lease_until"])


def downgrade() -> None:
    # Old code cannot safely resume an in-flight send: it may already be delivered.
    op.execute("UPDATE telegram_outbox SET status='failed', last_error="
               "'Исход доставки неизвестен после отката; проверьте Telegram перед повтором' "
               "WHERE status='sending'")
    op.drop_index("ix_telegram_outbox_lease", table_name="telegram_outbox")
    op.drop_index("ix_telegram_outbox_due", table_name="telegram_outbox")
    for name in ("telegram_message_id", "updated_at", "last_error_kind", "last_attempt_at", "lease_until", "claim_token", "next_attempt_at"):
        op.drop_column("telegram_outbox", name)
    op.drop_table("job_sync_failures")
    # The prior runner/UI do not know partial; preserve its diagnostic as failed.
    op.execute("UPDATE jobs SET status='failed' WHERE status='partial'")
    op.drop_column("jobs", "result_updated_at")
    op.drop_column("jobs", "result_json")
