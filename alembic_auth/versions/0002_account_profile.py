"""Persist profile contact links and repair legacy administrator roles.

Revision ID: 0002_account_profile
Revises: 0001_auth_baseline
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_account_profile"
down_revision: str | Sequence[str] | None = "0001_auth_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 基线按当前 metadata 建新库；新库可能已有此列，旧库则需要增量加列。
    op.execute(
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS "
        "contact_links JSON NOT NULL DEFAULT '[]'::json"
    )
    # 旧建号脚本写入 admin:admin；RBAC 已没有该角色，导致密码登录后 /me 403。
    op.execute(
        """
        UPDATE profiles AS p
        SET role = 'super_admin'
        FROM users AS u
        WHERE p.user_id = u.id
          AND u.account_level = 'admin'
          AND p.role = 'admin'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE profiles DROP COLUMN IF EXISTS contact_links")
