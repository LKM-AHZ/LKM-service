"""auth independent DB baseline

Revision ID: a0b1c2d3e4f5
Revises:
Create Date: 2026-09-13

auth 独立库全量基线（M3.B 真拆库后第一个正式迁移）。auth 表（users/profiles/
refresh_tokens/totp/... 共 18 张，见 app/modules/auth/models.py）挂 ``AuthBase``/
``auth_metadata``，已物理迁出单体 ``Base.metadata``，故业务库 Alembic 链不再覆盖它们，
由本第二迁移链负责。

实现取 ``auth_metadata.create_all(bind=...)``（checkfirst 幂等）：与 dev 的
``AuthBase.create_all`` 通道同源，避免手写 18 张表 DDL 与模型漂移；对「表已由 create_all
建出、现切 Alembic」的存量库亦安全（已存在的表跳过，仅补 alembic_version 版本戳）。后续
auth 表结构变更仍照常 ``alembic -c alembic.auth.ini revision --autogenerate`` 生成增量。
"""

from collections.abc import Sequence

from alembic import op
from auth.db.base import auth_metadata
from auth.register import register_models

# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f5"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema：建出 auth 库全部缺失表（幂等）。"""
    # env.py 的 target metadata 仅 import 空 auth_metadata，须先注册 auth models 才有表。
    register_models()
    auth_metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Downgrade schema：清空 auth 库本链所辖表。"""
    register_models()
    auth_metadata.drop_all(bind=op.get_bind())
