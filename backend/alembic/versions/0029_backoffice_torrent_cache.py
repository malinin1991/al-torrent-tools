"""Cache validated Nova torrent details independently of archive/SLA history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029_backoffice_torrent_cache"
down_revision = "0028_automatic_encoder"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "backoffice_torrent_cache",
        sa.Column("provider_key", sa.String(64), primary_key=True),
        sa.Column("release_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("torrent_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("index_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("backoffice_torrent_cache")
