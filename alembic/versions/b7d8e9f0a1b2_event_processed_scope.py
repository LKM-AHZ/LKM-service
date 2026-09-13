"""event_processed add scope, composite PK (scope, event_id)

Revision ID: b7d8e9f0a1b2
Revises: 6f2a4c8e0b1d3f5a
Create Date: 2026-09-13

M4 Pulsar 迁移：同一事件由多个订阅消费（points 拆 reward/stats/tasks 扇出）。原幂等账本
以 event_id 单列为主键，首个订阅记账后其余订阅会被误判「已处理」而跳过、静默丢副作用。
故加 scope 列（订阅名）并把主键改为复合 (scope, event_id)，各订阅独立记账。

存量行 scope 回填 'default'（对齐 app/db/event_processed.py DEFAULT_SCOPE，旧直发/单订阅
路径的兼容 scope）。downgrade 回退到 event_id 单主键，可能因跨 scope 同 event_id 冲突，
仅作运维兜底。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "6f2a4c8e0b1d3f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PK = "pk_event_processed"
_UNIQUE_INDEX = "ix_event_processed_event_id"

# 主键名在 alembic 路径（pk_event_processed）与 create_all 路径（PG 默认 event_processed_pkey）
# 下不一致，动态查找后 DROP，避免迁移因约束名不符而失败。
_DROP_PK_SQL = """
DO $$
DECLARE pkey_name text;
BEGIN
  SELECT conname INTO pkey_name FROM pg_constraint
  WHERE conrelid = 'event_processed'::regclass AND contype = 'p';
  IF pkey_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE event_processed DROP CONSTRAINT %I', pkey_name);
  END IF;
END $$;
"""


def upgrade() -> None:
    op.add_column(
        "event_processed",
        sa.Column(
            "scope",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
    )
    op.execute(f"DROP INDEX IF EXISTS {_UNIQUE_INDEX}")
    op.execute(_DROP_PK_SQL)
    op.create_primary_key(_PK, "event_processed", ["scope", "event_id"])
    # 回填完成后去掉 server_default，后续插入由 ORM 显式传 scope。
    op.alter_column("event_processed", "scope", server_default=None)


def downgrade() -> None:
    op.execute(_DROP_PK_SQL)
    op.create_primary_key(_PK, "event_processed", ["event_id"])
    op.create_index(_UNIQUE_INDEX, "event_processed", ["event_id"], unique=True)
    op.drop_column("event_processed", "scope")
