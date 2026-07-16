"""file_size в архиве: Integer → BigInteger (размеры >2ГБ)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_archive_file_size_bigint"
down_revision: str | None = "0002_archive_torrent_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "torrent_archive",
        "file_size",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "torrent_archive",
        "file_size",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
