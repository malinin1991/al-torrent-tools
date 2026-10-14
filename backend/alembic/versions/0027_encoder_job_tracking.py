"""Observe Kensetsu execution and repair provable legacy single-file receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0027_encoder_job_tracking"
down_revision = "0026_job_outcomes_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "encoder_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("encoder_submission_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_job_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("progress", sa.Float()),
        sa.Column("output_filename", sa.Text()),
        sa.Column("output_available", sa.Boolean()),
        sa.Column("output_size_bytes", sa.BigInteger()),
        sa.Column("remote_created_at", sa.DateTime()),
        sa.Column("remote_started_at", sa.DateTime()),
        sa.Column("remote_completed_at", sa.DateTime()),
        sa.Column("error_message", sa.Text()),
        sa.Column("poll_error", sa.Text()),
        sa.Column("poll_error_kind", sa.String(32)),
        sa.Column("poll_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime()),
        sa.Column("last_success_at", sa.DateTime()),
        sa.Column("output_checked_at", sa.DateTime()),
        sa.Column("next_poll_at", sa.DateTime(), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("item_id", "external_job_id", name="uq_encoder_job_item_external"),
        sa.CheckConstraint("status IN ('unknown','planned','in_progress','done','error','cancelled')", name="ck_encoder_job_status"),
    )
    op.create_index("ix_encoder_jobs_item_id", "encoder_jobs", ["item_id"])
    op.create_index("ix_encoder_jobs_due", "encoder_jobs", ["next_poll_at", "lease_until", "id"])
    # Frozen, narrow proof: one actually posted source, an explicit external ID,
    # exact filename echo, no errors or contradictory paths. No external POST.
    op.execute(sa.text("""
        WITH repaired AS (
            UPDATE encoder_submission_items i
            SET status='accepted', error=NULL,
                external_job_ids=jsonb_build_array(s.response_json->'job'->>'id')
            FROM encoder_submissions s
            WHERE i.submission_id=s.id AND i.status='unknown'
              AND i.error='Ответ не подтверждает приём именно этого файла'
              AND s.error IS NULL AND s.http_status IN (200,207)
              AND s.request_started_at IS NOT NULL AND s.finished_at IS NOT NULL
              AND jsonb_typeof(s.response_json->'job')='object'
              AND jsonb_typeof(s.response_json->'job'->'id') IN ('string','number')
              AND length(s.response_json->'job'->>'id') BETWEEN 1 AND 255
              AND COALESCE(s.response_json->'job'->>'path','')=''
              AND COALESCE(s.response_json->'job'->>'input_path','')=''
              AND COALESCE(s.response_json->'errors','null'::jsonb) IN ('null'::jsonb,'[]'::jsonb)
              AND COALESCE(s.response_json->'jobs','null'::jsonb) IN ('null'::jsonb,'[]'::jsonb)
              AND i.full_path<>''
              AND s.response_json->'job'->>'filename'=regexp_replace(i.full_path,'^.*/','')
              AND (SELECT count(*) FROM encoder_submission_items p
                   WHERE p.submission_id=s.id AND p.status<>'not_sent')=1
            RETURNING i.submission_id
        )
        UPDATE encoder_submissions SET updated_at=timezone('UTC',now())
        WHERE id IN (SELECT submission_id FROM repaired)
    """))
    op.execute(sa.text("""
        INSERT INTO encoder_jobs (item_id,external_job_id,status,poll_failures,next_poll_at,created_at,updated_at)
        SELECT DISTINCT i.id,j.value,'unknown',0,timezone('UTC',now()),timezone('UTC',now()),timezone('UTC',now())
        FROM encoder_submission_items i
        CROSS JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(i.external_job_ids)='array'
            THEN i.external_job_ids ELSE '[]'::jsonb END) AS j(value)
        WHERE i.status='accepted' AND length(j.value) BETWEEN 1 AND 255
        ON CONFLICT (item_id,external_job_id) DO NOTHING
    """))


def downgrade():
    op.drop_table("encoder_jobs")
