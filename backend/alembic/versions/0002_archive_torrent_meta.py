"""Поля описания и типа торрента в архиве."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_archive_torrent_meta"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("torrent_archive", sa.Column("torrent_description", sa.Text(), nullable=True))
    op.add_column("torrent_archive", sa.Column("torrent_type", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("torrent_archive", "torrent_type")
    op.drop_column("torrent_archive", "torrent_description")
