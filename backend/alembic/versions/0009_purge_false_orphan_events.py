"""Удаление ложных orphan-событий (скан по save_path года)."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

revision: str = "0009_purge_false_orphan_events"
down_revision: str | None = "0008_cleanup_rules_both"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Data-fix: привязан к текущей логике resolve_orphan_scan_root.
    bind = op.get_bind()
    with Session(bind=bind) as session:
        from app.services.db_maintenance import purge_false_orphan_events

        purge_false_orphan_events(session, commit=False)


def downgrade() -> None:
    # Удалённые события не восстановить.
    pass
