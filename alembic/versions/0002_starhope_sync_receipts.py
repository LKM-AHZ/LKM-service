"""Separate server receipt cursors from StarHope's client LWW timestamps."""

import sqlalchemy as sa

from alembic import op

revision = "0002_starhope_sync_receipts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TABLES = (
    "starhope_questions",
    "starhope_folders",
    "starhope_practice_sessions",
    "starhope_ai_agents",
)


def upgrade() -> None:
    for table in TABLES:
        # Existing rows must be visible to cursors issued before this migration.
        op.add_column(
            table,
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(f"ix_{table}_received_at", table, ["received_at"])
        op.alter_column(table, "received_at", server_default=None)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_received_at", table_name=table)
        op.drop_column(table, "received_at")
