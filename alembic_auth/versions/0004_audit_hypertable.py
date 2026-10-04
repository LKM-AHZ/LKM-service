"""Convert auth audit logs to a TimescaleDB hypertable without deleting history.

Existing tables require a primary key change. Run this revision in a maintenance
window: PostgreSQL takes an exclusive table lock while replacing that key.
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_audit_hypertable"
down_revision = "0003_session_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    primary_key = bind.scalar(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'audit_logs'::regclass AND contype = 'p'"
        )
    )
    if primary_key == "PRIMARY KEY (id)":
        op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_pkey")
        op.execute("ALTER TABLE audit_logs ADD PRIMARY KEY (created_at, id)")
    elif primary_key != "PRIMARY KEY (created_at, id)":
        raise RuntimeError(f"audit_logs 主键未知，停止迁移：{primary_key}")

    # 扩展缺失时仍保留可用的普通表。PostgreSQL 的异常块会回滚失败的 DDL。
    op.execute(
        """
        DO $$ BEGIN
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
    )


def downgrade() -> None:
    raise RuntimeError("TimescaleDB 不支持原地把 audit_logs hypertable 转回普通表")
