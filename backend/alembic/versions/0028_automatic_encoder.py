"""Persistent automatic encoder policies, readiness, queue and request receipts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_automatic_encoder"
down_revision = "0027_encoder_job_tracking"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "release_encoder_policies",
        sa.Column("release_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("video_id", sa.String(255)), sa.Column("audio_id", sa.String(255)),
        sa.Column("audio_downmix", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("participating", sa.Boolean(), nullable=False),
        sa.Column("presets_json", JSONB(), nullable=False),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "encoder_auto_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("info_hash", sa.String(128), nullable=False),
        sa.Column("baseline", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("release_id", "generation", "info_hash", name="uq_encoder_auto_source"),
    )
    op.create_table(
        "encoder_file_readiness",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("info_hash", sa.String(128), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("hash_algo", sa.String(32), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("ui_status", sa.String(16), nullable=False),
        sa.Column("job_id", sa.Integer()),
        sa.Column("prior_info_hash", sa.String(128)),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("info_hash", "relative_path", name="uq_encoder_readiness_source"),
    )
    op.create_table(
        "encoder_auto_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("info_hash", sa.String(128), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("block_reason", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("policy_revision", sa.Integer()),
        sa.Column("submission_id", sa.Integer()),
        sa.Column("receipt_item_id", sa.Integer()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("release_id", "generation", "info_hash", "relative_path", name="uq_encoder_auto_item"),
        sa.CheckConstraint("state IN ('waiting','ready','claimed','receipt_pending','accepted','needs_attention','cancelled')", name="ck_encoder_auto_state"),
    )
    op.create_table(
        "encoder_auto_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer()),
        sa.Column("release_id", sa.Integer()),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("details_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table, fields in {
        "encoder_auto_sources": ("release_id", "info_hash"),
        "encoder_file_readiness": ("info_hash",),
        "encoder_auto_items": ("release_id", "info_hash", "submission_id"),
        "encoder_auto_events": ("item_id", "release_id"),
    }.items():
        for field in fields:
            op.create_index(f"ix_{table}_{field}", table, [field])
    op.create_index("ix_encoder_auto_due", "encoder_auto_items", ["state", "next_attempt_at", "lease_until", "id"])
    for column in (
        sa.Column("origin", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("auto_item_id", sa.Integer()),
        sa.Column("policy_revision", sa.Integer()),
        sa.Column("payload_json", JSONB(), nullable=False, server_default="{}"),
        sa.Column("recovery_json", JSONB(), nullable=False, server_default="{}"),
        sa.Column("receipt_next_at", sa.DateTime()),
        sa.Column("receipt_checked_at", sa.DateTime()),
        sa.Column("receipt_error", sa.Text()),
    ):
        op.add_column("encoder_submissions", column)
    op.create_index("ix_encoder_submissions_auto_item_id", "encoder_submissions", ["auto_item_id"])
    op.create_index("ix_encoder_submissions_receipt_next_at", "encoder_submissions", ["receipt_next_at"])
    op.execute("INSERT INTO settings (key,value,updated_at) VALUES ('video_kensetsu_auto_enabled','false',timezone('UTC',now())) ON CONFLICT (key) DO NOTHING")


def downgrade():
    op.drop_index("ix_encoder_submissions_receipt_next_at", "encoder_submissions")
    op.drop_index("ix_encoder_submissions_auto_item_id", "encoder_submissions")
    for column in ("receipt_error", "receipt_checked_at", "receipt_next_at", "recovery_json", "payload_json", "policy_revision", "auto_item_id", "origin"):
        op.drop_column("encoder_submissions", column)
    for table in ("encoder_auto_events", "encoder_auto_items", "encoder_file_readiness", "encoder_auto_sources", "release_encoder_policies"):
        op.drop_table(table)
    op.execute("DELETE FROM settings WHERE key IN ('video_kensetsu_auto_enabled','encoder_auto_initialized','encoder_auto_heartbeat_at','encoder_auto_server_retry_at') OR key LIKE 'encoder_auto_hash_%'")
