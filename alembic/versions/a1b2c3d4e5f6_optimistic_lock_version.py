"""optimistic lock version columns

蓝图 §6.1 乐观锁：给业务表加 ``version`` 列（每次成功更新 +1，CAS 冲突返 409 + 当前值）。
本次覆盖三个可被端点直接编辑的聚合根：``content_items`` / ``boards`` / ``articles``。

``server_default='1'`` 是必需的：既有行要在 ADD COLUMN 时拿到初始版本号，且
``create_all`` 通道的 ``_sync_additive_schema``（§8 #38）只兜「可空或带 server_default」
的加列——NOT NULL 且无默认的缺列会被跳过并告警，既有部署上就会 UndefinedColumn。

Revision ID: a1b2c3d4e5f6
Revises: 72a6bdf65538
Create Date: 2026-09-27 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = '72a6bdf65538'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 加 version 列的既有表（均带 server_default 便于既有行回填）。
_TABLES: tuple[str, ...] = ("content_items", "boards", "articles")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "version")
