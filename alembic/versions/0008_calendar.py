"""Calendar events and subscriptions.

Revision ID: 0008_calendar
Revises: 0007_resource_features

- ``calendar_events``：用户日程（重复规则/标签/元数据存 JSONB，软删）。
- ``calendar_subscriptions``：订阅他人公开日历，(subscriber, target) 复合主键幂等。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_calendar"
down_revision: str | Sequence[str] | None = "0007_resource_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_all_day", sa.Boolean(), nullable=False),
        sa.Column("visibility", sa.Integer(), nullable=False),
        sa.Column(
            "recurrence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("icon_id", sa.String(length=36), nullable=True),
        sa.Column("background_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_events_owner_start",
        "calendar_events",
        ["owner_id", "start_time"],
    )
    op.create_index(
        "ix_calendar_events_owner_end",
        "calendar_events",
        ["owner_id", "end_time"],
    )

    op.create_table(
        "calendar_subscriptions",
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("subscriber_id", "target_id"),
    )


def downgrade() -> None:
    op.drop_table("calendar_subscriptions")
    op.drop_index("ix_calendar_events_owner_end", table_name="calendar_events")
    op.drop_index("ix_calendar_events_owner_start", table_name="calendar_events")
    op.drop_table("calendar_events")
