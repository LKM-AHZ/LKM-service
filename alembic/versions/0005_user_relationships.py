"""User block/mute relationships.

Revision ID: 0005_user_relationships
Revises: 0004_projects_collaboration

只做加性变更：新增 ``user_relationships`` 表。每行一个方向（``block`` 语义双向、
``mute`` 单向）；``UNIQUE(user_id, target_id, kind)`` 保证幂等。``user_id`` /
``target_id`` 为裸 UUID（跨库无物理外键）。

树洞是匿名身份，不接入账号 block/mute，故本迁移不涉及 treehole 表。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_user_relationships"
down_revision: str | Sequence[str] | None = "0004_projects_collaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_relationships",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_generate_v7()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "target_id", "kind", name="uq_user_relationships_kind"
        ),
    )
    op.create_index(
        "ix_user_relationships_user_kind", "user_relationships", ["user_id", "kind"]
    )
    op.create_index(
        "ix_user_relationships_target_kind",
        "user_relationships",
        ["target_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_relationships_target_kind", table_name="user_relationships"
    )
    op.drop_index(
        "ix_user_relationships_user_kind", table_name="user_relationships"
    )
    op.drop_table("user_relationships")
