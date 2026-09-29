"""revoked access tokens（关 Redis 持久化后的 jti 撤销权威面）

新增 ``revoked_access_tokens`` 表（见 ``auth/models.py::RevokedAccessToken``）。L2 关掉 AOF
后，``jti:block:{jti}`` 不再跨重启存活，而 admin 单设备登出**刻意不 bump token_version**
（避免连带踢掉该管理员其他设备），jti 是其唯一撤销依据 —— 故需要一张持久表兜底。

**幂等**：``0001_auth_baseline`` 走 ``auth_metadata.create_all``，对新库会直接建出本表
（模型已注册到 AuthBase），故这里先探存在性再建；否则新库上重复 CREATE 会失败。

Revision ID: 0002_revoked_access_tokens
Revises: 0001_auth_baseline
Create Date: 2026-09-29 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_revoked_access_tokens"
down_revision: str | Sequence[str] | None = "0001_auth_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "revoked_access_tokens"
_IX_EXPIRES = "ix_revoked_access_tokens_expires"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        # jti 直接作主键：查询即按 jti 命中，无需额外唯一索引
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 原 token 的 exp：到期后本行无意义，由清理任务按此列删
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_IX_EXPIRES, _TABLE, ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        op.drop_table(_TABLE)
