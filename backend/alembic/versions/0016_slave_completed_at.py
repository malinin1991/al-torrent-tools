"""torrent_pipeline.slave_completed_at — момент перехода в done (slave seeding)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0016_slave_completed_at"
down_revision: str | None = "0015_releases_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_pipeline",
        sa.Column("slave_completed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("torrent_pipeline", "slave_completed_at")
