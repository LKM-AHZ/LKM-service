"""Current auth schema baseline.

Revision ID: 0001_auth_baseline
Revises:

表结构取当前 auth_metadata；审计表的 TimescaleDB 装配在同一基线中完成。
原独立增量 ``0002_account_profile``（补 ``profiles.contact_links``、修复旧建号脚本写出的
无效 ``admin:admin`` 角色）已折入本基线：新库由 ``create_all`` 直接带出该列且无历史数据，
两条语句均为 no-op；仅「表已由 create_all 建过、再切 alembic 通道」的库才会真正生效。
"""

from collections.abc import Sequence

from alembic import op
from auth.db.base import auth_metadata
from auth.register import register_models
from core.db.shared_objects import UUID7_FUNCTION_SQL

# revision identifiers, used by Alembic.
revision: str = "0001_auth_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_TIMESCALE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
    PERFORM create_hypertable('audit_logs', 'created_at',
      chunk_time_interval => INTERVAL '7 days',
      if_not_exists => TRUE, migrate_data => TRUE);
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'audit_logs TimescaleDB 装配跳过：%', SQLERRM;
  END;
END $$;
"""


def upgrade() -> None:
    """Upgrade schema：建出 auth 库全部缺失表（幂等），并补齐既存表的折入增量。"""
    op.execute(UUID7_FUNCTION_SQL)
    register_models()
    if not auth_metadata.tables:
        raise RuntimeError("auth_metadata 为空：auth 模型未注册到 AuthBase，拒绝空盖章")
    auth_metadata.create_all(bind=op.get_bind())
    # create_all 不会修改已存在的表：既存库需补列（新库已由 metadata 带出，IF NOT EXISTS 兜底）。
    op.execute(
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS "
        "contact_links JSON NOT NULL DEFAULT '[]'::json"
    )
    # 旧建号脚本写入 admin:admin；RBAC 已没有该角色，导致密码登录后 /me 403。
    # 与 create_all 通道（auth/db/init.py::_create_auth_all）各留一份是有意的：两条通道互不依赖。
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
    op.execute(AUDIT_TIMESCALE_SQL)


def downgrade() -> None:
    """Downgrade schema：清空 auth 库本链所辖表。"""
    register_models()
    auth_metadata.drop_all(bind=op.get_bind())
