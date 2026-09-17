"""add outbox_archived table

Revision ID: c8e1a2b3d4f5
Revises: b7d8e9f0a1b2
Create Date: 2026-09-17

outbox 已发布冷表（M6.3）：relay 按保留期把已 published 且超期的行先复制到此表再从
outbox_events 删除（先归档后删），避免 outbox_events 无限增长。列对齐
app/db/outbox_archive.py OutboxArchived —— 与 outbox_events 同构，另加 archived_at
区分「投出时刻」与「归档时刻」。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c8e1a2b3d4f5"
down_revision: str | Sequence[str] | None = "b7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_archived",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("routing_key", sa.String(length=64), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_archived_event_id", "outbox_archived", ["event_id"])
    # 归档清理/按保留期复查的扫描路径
    op.create_index(
        "ix_outbox_archived_published_at", "outbox_archived", ["published_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_archived_published_at", table_name="outbox_archived")
    op.drop_index("ix_outbox_archived_event_id", table_name="outbox_archived")
    op.drop_table("outbox_archived")
