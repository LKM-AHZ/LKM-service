"""Friend relationships and temporary block/mute expiry.

Revision ID: 0009_friend_relationships
Revises: 0008_calendar

只做加性变更：

- 新增 ``friend_relationships``：一行一个方向（发起人 → 目标），``status`` 0 待接受 /
  100 好友 / 200 亲密好友；待接受请求的 ``expires_at`` 到期后不再出现在请求列表。
- ``user_relationships`` 增加 ``expires_at`` / ``degrade_to_status``：临时 block 到期
  降级为 mute（``degrade_to_status=-50``），空值表示永久。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_friend_relationships"
down_revision: str | Sequence[str] | None = "0008_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "friend_relationships",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("related_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "related_id", name="uq_friend_relationships_pair"
        ),
    )
    op.create_index(
        "ix_friend_relationships_account_status",
        "friend_relationships",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_friend_relationships_related_status",
        "friend_relationships",
        ["related_id", "status"],
    )

    op.add_column(
        "user_relationships",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_relationships",
        sa.Column("degrade_to_status", sa.SmallInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_relationships", "degrade_to_status")
    op.drop_column("user_relationships", "expires_at")
    op.drop_index(
        "ix_friend_relationships_related_status",
        table_name="friend_relationships",
    )
    op.drop_index(
        "ix_friend_relationships_account_status",
        table_name="friend_relationships",
    )
    op.drop_table("friend_relationships")
