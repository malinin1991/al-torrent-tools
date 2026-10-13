"""Durable per-file receipts for Video Kensetsu submissions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_encoder_history"
down_revision = "0024_backoffice_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "encoder_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False, unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("video_id", sa.String(255), nullable=False),
        sa.Column("audio_id", sa.String(255), nullable=False),
        sa.Column("audio_downmix", sa.String(16), nullable=False),
        sa.Column("presets_json", postgresql.JSONB(), nullable=False),
        sa.Column("retry_of_id", sa.Integer(), sa.ForeignKey("encoder_submissions.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("request_started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_encoder_submissions_created", "encoder_submissions", ["created_at", "id"])
    op.create_table(
        "encoder_submission_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Integer(), sa.ForeignKey("encoder_submissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer()),
        sa.Column("torrent_id", sa.Integer()),
        sa.Column("info_hash", sa.String(128), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("source_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("external_job_ids", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("submission_id", "file_id", name="uq_encoder_submission_file"),
        sa.CheckConstraint("status IN ('sending', 'accepted', 'not_sent', 'rejected', 'unknown')", name="ck_encoder_item_status"),
    )
    for name in ("submission_id", "release_id", "info_hash", "status"):
        op.create_index(f"ix_encoder_submission_items_{name}", "encoder_submission_items", [name])


def downgrade() -> None:
    op.drop_table("encoder_submission_items")
    op.drop_table("encoder_submissions")
