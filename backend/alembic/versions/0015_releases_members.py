"""Таблицы releases + release_members — карточка и состав релиза."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_releases_members"
down_revision: str | None = "0014_api_created_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("release_alias", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column(
            "genres_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_blocked_by_geo", sa.Boolean(), nullable=True),
        sa.Column("is_blocked_by_copyrights", sa.Boolean(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("release_id"),
    )
    op.create_index("ix_releases_release_alias", "releases", ["release_alias"])

    op.create_table(
        "release_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("api_member_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("role_label", sa.String(length=64), nullable=False),
        sa.Column("nickname", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["releases.release_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "role",
            "nickname",
            name="uq_release_members_release_role_nickname",
        ),
    )
    op.create_index("ix_release_members_release_id", "release_members", ["release_id"])
    op.create_index("ix_release_members_nickname", "release_members", ["nickname"])
    op.create_index("ix_release_members_role", "release_members", ["role"])


def downgrade() -> None:
    op.drop_index("ix_release_members_role", table_name="release_members")
    op.drop_index("ix_release_members_nickname", table_name="release_members")
    op.drop_index("ix_release_members_release_id", table_name="release_members")
    op.drop_table("release_members")
    op.drop_index("ix_releases_release_alias", table_name="releases")
    op.drop_table("releases")
