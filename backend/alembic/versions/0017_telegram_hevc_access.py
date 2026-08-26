"""Доступ второго Telegram-бота, маршрутизация outbox и original title."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0017_telegram_hevc_access"
down_revision: str | None = "0016_slave_completed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column("original_title", sa.String(length=512), nullable=True),
    )

    op.add_column(
        "telegram_outbox",
        sa.Column(
            "bot_key",
            sa.String(length=32),
            nullable=False,
            server_default="primary",
        ),
    )
    op.add_column(
        "telegram_outbox",
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_telegram_outbox_bot_key",
        "telegram_outbox",
        ["bot_key"],
    )
    op.create_unique_constraint(
        "uq_telegram_outbox_bot_dedupe",
        "telegram_outbox",
        ["bot_key", "dedupe_key"],
    )

    op.create_table(
        "telegram_bot_access",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bot_key", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "subject_type IN ('user', 'chat')",
            name="ck_telegram_bot_access_subject_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_telegram_bot_access_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_key",
            "subject_type",
            "telegram_id",
            name="uq_telegram_bot_access_subject",
        ),
    )
    op.create_index(
        "ix_telegram_bot_access_bot_subject_status",
        "telegram_bot_access",
        ["bot_key", "subject_type", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_bot_access_bot_subject_status",
        table_name="telegram_bot_access",
    )
    op.drop_table("telegram_bot_access")
    op.drop_constraint(
        "uq_telegram_outbox_bot_dedupe",
        "telegram_outbox",
        type_="unique",
    )
    op.drop_index("ix_telegram_outbox_bot_key", table_name="telegram_outbox")
    op.drop_column("telegram_outbox", "dedupe_key")
    op.drop_column("telegram_outbox", "bot_key")
    op.drop_column("releases", "original_title")
