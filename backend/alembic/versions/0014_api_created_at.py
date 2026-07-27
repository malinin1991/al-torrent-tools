"""Колонка torrent_archive.api_created_at — дата загрузки торрента AniLibria.

Backfill без offline-джоба: process_release (full_sync/ongoing) запрашивает
created_at в include и заполняет пустые api_created_at из list payload;
save_torrent не затирает поле None при отсутствии/битом created_at.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0014_api_created_at"
down_revision: str | None = "0013_ignore_hevc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_archive",
        sa.Column("api_created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("torrent_archive", "api_created_at")
