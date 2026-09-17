"""add interaction favorites / view logs

Revision ID: d9e0f1a2b3c4
Revises: c8e1a2b3d4f5
Create Date: 2026-09-17

interaction 域（M6.6）两张表，列对齐 app/modules/interaction/models.py：

- interaction_favorites：复合主键 (content_id, user_id) 保证收藏幂等；FK 指向
  content_items 并带 ON DELETE CASCADE（内容删除时明细不残留，计数无意义）。
- interaction_view_logs：代理主键 + (user_id, content_id) 唯一约束，浏览上报走
  ON CONFLICT DO UPDATE 刷新 viewed_at；viewed_at 单列索引供保留期清理扫描。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8e1a2b3d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interaction_favorites",
        sa.Column("content_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["content_id"], ["content_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("content_id", "user_id"),
    )
    op.create_index(
        "ix_interaction_fav_user_created",
        "interaction_favorites",
        ["user_id", "created_at"],
    )

    op.create_table(
        "interaction_view_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("content_id", sa.Integer(), nullable=False),
        sa.Column(
            "viewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["content_id"], ["content_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "content_id", name="uq_interaction_view_user_content"
        ),
    )
    op.create_index(
        "ix_interaction_view_user_viewed",
        "interaction_view_logs",
        ["user_id", "viewed_at"],
    )
    op.create_index(
        "ix_interaction_view_viewed", "interaction_view_logs", ["viewed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_interaction_view_viewed", table_name="interaction_view_logs")
    op.drop_index("ix_interaction_view_user_viewed", table_name="interaction_view_logs")
    op.drop_table("interaction_view_logs")
    op.drop_index("ix_interaction_fav_user_created", table_name="interaction_favorites")
    op.drop_table("interaction_favorites")
