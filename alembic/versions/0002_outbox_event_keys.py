"""Global outbox event keys and idempotent dead letter persistence.

Revision ID: 0002_outbox_event_keys
Revises: 0001_uuid_baseline
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_outbox_event_keys"
down_revision = "0001_uuid_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_event_keys",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # No full scan/backfill of potentially huge hypertables during deployment.
    # enqueue_outbox checks legacy indexed tables when an explicit key first appears.
    op.add_column("dlq_messages", sa.Column("source_message_id", sa.String(255)))
    op.create_index(
        "uq_dlq_source_message_id", "dlq_messages", ["source_message_id"], unique=True
    )
    op.add_column(
        "event_failures", sa.Column("replayed_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("event_failures", "replayed_at")
    op.drop_index("uq_dlq_source_message_id", table_name="dlq_messages")
    op.drop_column("dlq_messages", "source_message_id")
    op.drop_table("outbox_event_keys")
