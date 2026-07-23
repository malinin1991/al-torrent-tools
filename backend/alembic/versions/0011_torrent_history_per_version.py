"""История торрентов: superseded + info_hash на file_change_events."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011_torrent_history_per_version"
down_revision: str | None = "0010_torrent_file_ui_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "torrent_archive",
        sa.Column(
            "superseded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index("ix_torrent_archive_superseded", "torrent_archive", ["superseded"])

    op.add_column(
        "file_change_events",
        sa.Column("info_hash", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_file_change_events_info_hash", "file_change_events", ["info_hash"])

    # Legacy events: info_hash из актуальной (не superseded) версии torrent_id.
    op.execute(
        """
        UPDATE file_change_events AS e
        SET info_hash = a.info_hash
        FROM (
            SELECT DISTINCT ON (torrent_id) torrent_id, info_hash
            FROM torrent_archive
            ORDER BY torrent_id, superseded ASC, id DESC
        ) AS a
        WHERE e.info_hash IS NULL
          AND e.torrent_id IS NOT NULL
          AND e.torrent_id = a.torrent_id
        """
    )

    # Sticky ui_status из последнего события по (info_hash, relative_path).
    op.execute(
        """
        UPDATE torrent_files AS tf
        SET ui_status = CASE
            WHEN ev.kind = 'added' THEN 'new'
            WHEN ev.kind = 'modified' THEN 'changed'
            ELSE tf.ui_status
        END
        FROM (
            SELECT DISTINCT ON (info_hash, relative_path)
                info_hash, relative_path, kind
            FROM file_change_events
            WHERE info_hash IS NOT NULL
              AND relative_path IS NOT NULL
              AND kind IN ('added', 'modified')
            ORDER BY info_hash, relative_path, id DESC
        ) AS ev
        WHERE tf.info_hash = ev.info_hash
          AND tf.relative_path = ev.relative_path
        """
    )

def downgrade() -> None:
    op.drop_index("ix_file_change_events_info_hash", table_name="file_change_events")
    op.drop_column("file_change_events", "info_hash")
    op.drop_index("ix_torrent_archive_superseded", table_name="torrent_archive")
    op.drop_column("torrent_archive", "superseded")
