"""Колонка torrent_archive.ignore_hevc — ручной игнор HEVC-пары для AVC."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013_ignore_hevc"
down_revision: str | None = "0012_pipeline_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_archive",
        sa.Column(
            "ignore_hevc",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("torrent_archive", "ignore_hevc")
