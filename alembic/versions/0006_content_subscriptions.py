"""Unified content subscriptions + per-post watch.

Revision ID: 0006_content_subscriptions
Revises: 0005_user_relationships

- 新增 ``content_subscriptions``：``target_type`` 取 ``board|tag|column``，
  ``target_id`` 存字符串（board/column 为 UUID 字符串，tag 为归一化标签）；
  ``UNIQUE(user_id, target_type, target_id)`` + 软删墓碑支持幂等重订阅。
- 新增 ``post_watches``：单帖 watch，``UNIQUE(user_id, content_id)`` + 软删墓碑。
- 把 ``board_follows`` 的活动行搬进 ``content_subscriptions(target_type='board')``，
  随后删除旧表（写入路径已在同一版本的应用代码里切换到统一订阅）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_content_subscriptions"
down_revision: str | Sequence[str] | None = "0005_user_relationships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_subscriptions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=10), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("notify_level", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "target_type",
            "target_id",
            name="uq_content_subscriptions_target",
        ),
    )
    op.create_index(
        "ix_content_subscriptions_target",
        "content_subscriptions",
        ["target_type", "target_id"],
    )

    op.create_table(
        "post_watches",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("notify_level", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["content_id"], ["content_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_id", name="uq_post_watches_pair"),
    )
    op.create_index("ix_post_watches_content", "post_watches", ["content_id"])

    # 板块关注 → 统一订阅（保留软删墓碑与创建时间，语义等价）
    op.execute(
        """
        INSERT INTO content_subscriptions
            (user_id, target_type, target_id, notify_level, created_at, deleted_at)
        SELECT follower_id, 'board', board_id::text, 'all', created_at, deleted_at
        FROM board_follows
        """
    )
    op.drop_index("ix_board_follows_board", table_name="board_follows")
    op.drop_table("board_follows")


def downgrade() -> None:
    op.create_table(
        "board_follows",
        sa.Column("follower_id", sa.Uuid(), nullable=False),
        sa.Column("board_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["board_id"], ["boards.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("follower_id", "board_id", name="uq_board_follows_pair"),
    )
    op.create_index(
        "ix_board_follows_board", "board_follows", ["board_id"]
    )
    op.execute(
        """
        INSERT INTO board_follows
            (follower_id, board_id, created_at, deleted_at)
        SELECT user_id, target_id::uuid, created_at, deleted_at
        FROM content_subscriptions
        WHERE target_type = 'board'
        """
    )

    op.drop_index("ix_post_watches_content", table_name="post_watches")
    op.drop_table("post_watches")
    op.drop_index(
        "ix_content_subscriptions_target", table_name="content_subscriptions"
    )
    op.drop_table("content_subscriptions")
