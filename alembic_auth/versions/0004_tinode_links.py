"""tinode_links：LKM 用户与 Tinode 账号的映射（聊天身份桥）。

Revision ID: 0004_tinode_links
Revises: 0003_auth_oauth_desktop

新库由 ``create_all`` 直接带出该表；本迁移给「表已由 create_all 建过、再切 alembic
通道」的既存库补表，全部幂等（``IF NOT EXISTS``）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_tinode_links"
down_revision: str | Sequence[str] | None = "0003_auth_oauth_desktop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tinode_links (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tinode_uid VARCHAR(32) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tinode_links")
