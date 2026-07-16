"""Таблица release_checkpoints для пропуска неизменённых релизов."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005_release_checkpoints"
down_revision: str | None = "0004_torrent_type_len"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_checkpoints",
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("api_updated_at", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("api_fresh_at", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("torrents_fingerprint", sa.Text(), nullable=False, server_default=""),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("release_id"),
    )


def downgrade() -> None:
    op.drop_table("release_checkpoints")
