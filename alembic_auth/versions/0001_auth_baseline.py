"""Current auth schema baseline.

Revision ID: 0001_auth_baseline
Revises:

表结构取当前 auth_metadata；审计表的 TimescaleDB 装配在同一基线中完成。
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
    """Upgrade schema：建出 auth 库全部缺失表（幂等）。"""
    op.execute(UUID7_FUNCTION_SQL)
    register_models()
    if not auth_metadata.tables:
        raise RuntimeError("auth_metadata 为空：auth 模型未注册到 AuthBase，拒绝空盖章")
    auth_metadata.create_all(bind=op.get_bind())
    op.execute(AUDIT_TIMESCALE_SQL)


def downgrade() -> None:
    """Downgrade schema：清空 auth 库本链所辖表。"""
    register_models()
    auth_metadata.drop_all(bind=op.get_bind())
