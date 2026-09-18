"""add counts_reconciled_at to content_items

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-18

M6.10 互动计数 Redis 链路的对账标记列：本行计数最后一次**因对账被修正**的时刻
（NULL = 从未偏差）。语义刻意不是「上次扫描时刻」——只写偏差行，使「连续两次对账，
第二次 affected == 0」成为可证伪的收敛断言。

注：``view_count`` 不在本链路内（无明细行可重算），故无对应标记。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "content_items",
        sa.Column("counts_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("content_items", "counts_reconciled_at")
