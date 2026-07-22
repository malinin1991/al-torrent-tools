"""Cleanup rules: target_client master → both для Cleanup Slave."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_cleanup_rules_both"
down_revision: str | None = "0007_file_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # После разделения cleanup_master/cleanup_slave правила только на master
    # молча пропускались slave-джобой (checked=0). Типичный сид — оба клиента.
    op.execute(
        """
        UPDATE cleanup_rules
        SET target_client = 'both'
        WHERE target_client = 'master'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE cleanup_rules
        SET target_client = 'master'
        WHERE target_client = 'both'
        """
    )
