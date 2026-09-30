"""points_ledger → hypertable（为 continuous aggregate 铺路）

蓝图目标篇「**PG 连续聚合视图**（度量/行为报表）」的落地。`points_ledger` 是纯 append
流水、且**无任何 FK 引用它**，是本仓唯一既能转 hypertable 又有真实报表需求的业务表——
被 FK 引用的 `content_items`（4 处）/ auth `users`（12 处）/ 自引用的 `content_comments`
都转不了，因为 hypertable 的**每个唯一索引都必须包含分区列**。

本迁移做两件事（**都限定在常规迁移事务内**）：

1. 主键 `id` → 复合主键 `(created_at, id)`（分区列必须出现在主键里）；
2. 幂等键唯一约束 `(user_id, ref_type, ref_id)` → 并入 `created_at`（**约束名不变**，
   `pg_upsert` 按名解析 arbiter），再加 DO 块能力探测式 `create_hypertable`。

**continuous aggregate 刻意不在这里建**：`CREATE MATERIALIZED VIEW ... WITH
(timescaledb.continuous)`（隐含 `WITH DATA`）在**任何事务块内**都不允许执行——连
`DO $$ … $$` 的隐式事务都过不去（TimescaleDB 内部 `IsTransactionBlock()` 在 DO 块里恒为真，
`op.get_context().autocommit_block()` 也救不了，实测报 "cannot run inside a transaction
block"）。故 `points_daily` 由**应用启动时**的
`app/db/init_db.py::_ensure_continuous_aggregates`（自开 AUTOCOMMIT 连接、顶级语句）装配，
两条通道（`create_all` 与 Alembic）共用同一段代码。本迁移只负责把表变成**能承载 cagg 的
hypertable**。

**幂等弱化的已知面**（与 `outbox_events` 同款取舍）：第 2 步后同一
`(user_id, ref_type, ref_id)` 的两次投递因 `created_at` 不同而不再撞唯一约束，DB 级兜底
由「强保证」降为「同微秒才生效」；真实幂等由 `points/service.py::reward` 的**按用户行锁 +
`get_by_ref` 预检**承担。详见 `points/models.py` 的类 docstring。

Revision ID: 0004_points_ledger_cagg
Revises: 0003_upload_sessions
Create Date: 2026-09-30 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
# 注意 revision id 会写进 ``alembic_version.version_num``（varchar(32)），**不得超过 32 字符**。
revision: str = "0004_points_ledger_cagg"
down_revision: str | Sequence[str] | None = "0003_upload_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "points_ledger"
_PK = "points_ledger_pkey"
_UQ = "uq_points_ledger_ref"
_CAGG = "points_daily"

# DO 块做能力探测——与 0001_uuid_baseline.py 的 TIMESCALE_DDL 同款：扩展不存在时只告警、
# 表保持普通表（主键多一列 created_at 无副作用，DML 语义不变）；内层 BEGIN...EXCEPTION 是
# 隐式 savepoint，失败不污染外层迁移事务。此处**只放事务内可做的装配**（扩展 + hypertable，
# 后者是普通函数调用，DO 块内合法）；cagg 见模块 docstring。
TIMESCALE_DDL = f"""
DO $$
BEGIN
  BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'timescaledb 不可用，points_ledger 保持普通表：%', SQLERRM;
  END;

  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    BEGIN
      PERFORM create_hypertable('{_TABLE}', 'created_at',
          chunk_time_interval => INTERVAL '7 days',
          if_not_exists => TRUE, migrate_data => TRUE);
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'timescaledb 装配失败，points_ledger 保持普通表：%', SQLERRM;
    END;
  END IF;
END $$;
"""

# 退装：TimescaleDB **没有**把 hypertable 转回普通表的反向转换，故已转 hypertable 的库上
# 主键/唯一约束不可逆——此时只拆 cagg 并告警（完整回退须重建该表）；普通表（扩展缺失或从未
# 转成）上约束可正常回退到单列主键。
DOWNGRADE_DDL = f"""
DO $$
DECLARE
  is_ht boolean := false;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    BEGIN
      EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS {_CAGG}';
      SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                     WHERE hypertable_name = '{_TABLE}') INTO is_ht;
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'timescaledb 退装失败（按普通表处理）：%', SQLERRM;
    END;
  END IF;

  IF is_ht THEN
    RAISE WARNING 'points_ledger 已是 hypertable，约束不可逆；本 downgrade 仅拆 continuous '
                  'aggregate，完整回退须重建该表';
  ELSE
    EXECUTE 'ALTER TABLE {_TABLE} DROP CONSTRAINT {_UQ}';
    EXECUTE 'ALTER TABLE {_TABLE} ADD CONSTRAINT {_UQ} '
            'UNIQUE (user_id, ref_type, ref_id)';
    EXECUTE 'ALTER TABLE {_TABLE} DROP CONSTRAINT {_PK}';
    EXECUTE 'ALTER TABLE {_TABLE} ADD CONSTRAINT {_PK} PRIMARY KEY (id)';
  END IF;
END $$;
"""


def upgrade() -> None:
    """先改约束，再装配 Timescale（顺序不可颠倒）。"""
    # create_hypertable 要求分区列出现在表上的每个唯一索引里——PK/幂等约束必须先改，
    # 否则 Timescale 会拒绝转换。
    op.drop_constraint(_PK, _TABLE, type_="primary")
    op.create_primary_key(_PK, _TABLE, ["created_at", "id"])
    op.drop_constraint(_UQ, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UQ, _TABLE, ["user_id", "ref_type", "ref_id", "created_at"]
    )
    op.execute(TIMESCALE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
