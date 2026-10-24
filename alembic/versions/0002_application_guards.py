"""申请防重与冷却：专栏 pending 唯一、板块 slug 释放、审核查询索引。

背景（P0-2）：
- ``column_applications`` 允许多条 pending：同一用户可并发提交重复申请。加**部分唯一
  索引** ``(user_id) WHERE status='pending'`` 收口；存量重复 pending（历史脏数据）在
  建索引前按 user_id 保留最新一条，其余降级为 rejected 并在 review_note 标记迁移来源。
- ``board_applications.slug`` 原为全局唯一：一条申请被拒后 slug 被永久占用，用户既不能
  用同 slug 重提、别人也不能用。改为**部分唯一索引** ``slug WHERE status<>'rejected'``：
  被拒行释放标识，pending/approved 仍唯一。
- 冷却（被拒后 24h）不新增列：按 ``reviewed_at`` 计算；补 (user_id,status) /
  (applicant_id,slug) 查询索引。

Revision ID: 0002_application_guards
Revises: 0001_uuid_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_application_guards"
down_revision: str | Sequence[str] | None = "0001_uuid_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 存量重复 pending 的收敛：保留 id 最大（uuid7 最新）的一条，其余降级为 rejected。
# 不删除任何行；review_note 标注 migration 来源，便于人工复核。
_DEDUPE_PENDING_COLUMNS_SQL = """
WITH ranked AS (
    SELECT id,
           row_number() OVER (PARTITION BY user_id ORDER BY id DESC) AS rn
    FROM column_applications
    WHERE status = 'pending'
)
UPDATE column_applications AS ca
SET status = 'rejected',
    reviewed_at = now(),
    review_note = COALESCE(ca.review_note || ' ', '')
                  || '[migration] superseded duplicate pending application'
FROM ranked AS r
WHERE ca.id = r.id AND r.rn > 1
"""

# 约束名可能是 Postgres 默认 board_applications_slug_key，也可能被改名；
# 按定义查真名，避免硬编码在异构环境失手。
_DROP_SLUG_UNIQUE_SQL = """
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname
    FROM pg_constraint
    WHERE conrelid = 'board_applications'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) = 'UNIQUE (slug)';
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE board_applications DROP CONSTRAINT %I', cname);
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_DEDUPE_PENDING_COLUMNS_SQL)
    op.create_index(
        "uq_column_applications_pending_user",
        "column_applications",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_column_applications_user_status",
        "column_applications",
        ["user_id", "status"],
        unique=False,
    )

    op.execute(_DROP_SLUG_UNIQUE_SQL)
    op.create_index(
        "uq_board_applications_slug_active",
        "board_applications",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("status <> 'rejected'"),
    )
    op.create_index(
        "ix_board_applications_applicant_slug",
        "board_applications",
        ["applicant_id", "slug"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_board_applications_applicant_slug", table_name="board_applications")
    op.drop_index("uq_board_applications_slug_active", table_name="board_applications")
    # 回滚前若存在同 slug 的多条 rejected 行，重建全局唯一会失败——这是有意的：
    # 宁可报错要求人工决策，也不静默删历史行。
    op.create_unique_constraint(
        "board_applications_slug_key", "board_applications", ["slug"]
    )

    op.drop_index("ix_column_applications_user_status", table_name="column_applications")
    op.drop_index(
        "uq_column_applications_pending_user", table_name="column_applications"
    )
