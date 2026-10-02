"""Add many-to-many user-role assignments for core RBAC.

Revision ID: 0002_user_roles
Revises: 0001_auth_baseline
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_user_roles"
down_revision: str | Sequence[str] | None = "0001_auth_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 基线用当前 auth_metadata.create_all(checkfirst=True)：新库运行基线时
    # 已可能建出本表，故增量迁移也须能跳过；历史库则在这里补建。
    op.execute(
        """CREATE TABLE IF NOT EXISTS user_roles (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_name VARCHAR(40) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (user_id, role_name)
        )"""
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_roles_role_name_user_id "
        "ON user_roles (role_name, user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_roles")
