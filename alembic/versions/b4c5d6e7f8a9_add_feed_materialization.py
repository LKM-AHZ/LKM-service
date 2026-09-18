"""add feed materialization (feed_items + feed_fanout_state)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-18

M6.11 时间线物化读模型：

- ``feed_items``：一条 = 某用户 feed 里的一条内容（写扩散产物）。唯一约束
  ``(user_id, item_type, source_id)`` 让重复 fanout 幂等；``(user_id, created_at, id)``
  索引服务读路径游标。展示字段（title/content_preview/url）存快照，读路径零回查。
- ``feed_fanout_state``：每源 ``(created_at, id)`` 水位（cron 增量扫描的锚点）。

存量内容不回填（水位起点为「启用时刻」）；新关注走 ``backfill_author``/``backfill_board``
补最近 N 条——取舍见路线图 §8。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feed_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("board_id", sa.Integer(), nullable=True),
        sa.Column("sort_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "content_preview", sa.String(length=300), nullable=False, server_default=""
        ),
        sa.Column("url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "item_type", "source_id", name="uq_feed_item"
        ),
    )
    op.create_index(
        "ix_feed_items_user_cursor", "feed_items", ["user_id", "created_at", "id"]
    )
    op.create_index(
        "ix_feed_items_source", "feed_items", ["item_type", "source_id"]
    )

    op.create_table(
        "feed_fanout_state",
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("last_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("source"),
    )


def downgrade() -> None:
    op.drop_table("feed_fanout_state")
    op.drop_index("ix_feed_items_source", table_name="feed_items")
    op.drop_index("ix_feed_items_user_cursor", table_name="feed_items")
    op.drop_table("feed_items")
