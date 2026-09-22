"""建表前必须就绪的 **库级共享对象**（业务库与 auth 库各自一份）。

拆库后两条建库链（业务库 ``init_db`` / auth 库 ``auth.db.init``）都需要同一批
PG 级前置对象，故从 ``app/db/init_db.py`` 抽出为共享工具，避免任何一侧反向依赖另一侧。

本模块不得 import 业务模块或 auth 包。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lkm.db.shared_objects")

# PG 并发建同一对象时的冲突特征：多进程 / 多 xdist worker 同时首次建库，
# CREATE EXTENSION 与 CREATE OR REPLACE FUNCTION 之间仍会撞目录唯一约束
_CONCURRENT_DDL_MARKERS = ("already exists", "duplicate key", "concurrently updated")

UUID7_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.uuid_generate_v7() RETURNS uuid AS $$
DECLARE
  us bigint;
  b  bytea;
BEGIN
  us := (extract(epoch FROM clock_timestamp()) * 1000000)::bigint;
  b  := uuid_send(gen_random_uuid());
  b  := overlay(b PLACING substring(int8send(us >> 12) FROM 3) FROM 1 FOR 6);
  b  := set_byte(b, 6, (112 + ((us >> 8) & 15))::int);
  b  := set_byte(b, 7, (us & 255)::int);
  b  := set_byte(b, 8, (get_byte(b, 8) & 63) + 128);
  RETURN encode(b, 'hex')::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""


async def _run_shared_ddl(conn: Any, sql: str, what: str) -> None:
    """执行一条共享对象 DDL，容忍「并发下已被别的进程建好」。

    本函数被多个进程/多个 xdist worker 在启动期并发调用，而 PG 对
    ``CREATE EXTENSION`` / ``CREATE OR REPLACE FUNCTION`` 的目录写入并非完全可并发
    （实测会撞 duplicate key / "tuple concurrently updated"）。这类失败等于「别人已建好」，
    按成功处理并告警；其余异常（权限不足等）照抛，由调用方以真实原因失败。
    """
    import sqlalchemy as sa
    from sqlalchemy.exc import IntegrityError, ProgrammingError

    sp = await conn.begin_nested()
    try:
        await conn.execute(sa.text(sql))
        await sp.commit()
    except (ProgrammingError, IntegrityError) as exc:
        await sp.rollback()
        msg = str(getattr(exc, "orig", exc)).lower()
        if any(marker in msg for marker in _CONCURRENT_DDL_MARKERS):
            logger.warning("%s 已由并发建库方创建，跳过：%s", what, msg[:200])
            return
        raise


async def ensure_shared_objects(conn: Any) -> None:
    """建表前必须就绪的库级共享对象（幂等）。

    1. ``pg_trgm`` 扩展：M6.9 trgm 索引的 opclass 依赖它，索引 DDL 显式写
       ``public.gin_trgm_ops``，故扩展须在 public（schema-per-test 的 search_path 不含 public）。
    2. ``public.uuid_generate_v7()``：UUID 主键列的 ``server_default`` 目标（RFC 9562 uuid7，
       时间有序）。**必须在 ``create_all`` 之前建**——PG 建表即解析 DEFAULT 表达式，
       函数不存在会直接报错。建在 public，故模型侧 ``server_default`` 显式限定 schema。

    ``gen_random_uuid()`` 自 PG13 起是 core 内置，无需 pgcrypto 扩展。
    """
    import sqlalchemy as sa

    # pg_trgm 必须落在 public：索引 DDL 写死了 ``public.gin_trgm_ops``。直接
    # ``CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public`` 在「扩展已存在于别的
    # schema」时会静默什么都不做（SCHEMA 子句被忽略），直到建索引才以
    # 「operator class public.gin_trgm_ops does not exist」这种不知所云的错炸开，
    # 故先查实际 schema，必要时显式迁到 public。
    ext_schema = await conn.scalar(
        sa.text(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace"
            " WHERE e.extname = 'pg_trgm'"
        )
    )
    if ext_schema is None:
        await _run_shared_ddl(conn, "CREATE EXTENSION pg_trgm SCHEMA public", "pg_trgm")
    elif ext_schema != "public":
        logger.warning(
            "pg_trgm 装在 schema %s，迁到 public（trgm 索引 DDL 依赖 public.gin_trgm_ops）",
            ext_schema,
        )
        await conn.execute(sa.text("ALTER EXTENSION pg_trgm SET SCHEMA public"))
    await _run_shared_ddl(conn, UUID7_FUNCTION_SQL, "uuid_generate_v7")
