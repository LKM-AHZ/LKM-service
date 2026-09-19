"""建表前必须就绪的 **库级共享对象**（业务库与 auth 库各自一份）。

拆库后两条建库链（业务库 ``init_db`` / auth 库 ``auth.db.init``）都需要同一批
PG 级前置对象，故从 ``app/db/init_db.py`` 抽出为共享工具，避免任何一侧反向依赖另一侧。

本模块不得 import 业务模块或 auth 包。
"""

from __future__ import annotations

from typing import Any

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

    await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public"))
    await conn.execute(sa.text(UUID7_FUNCTION_SQL))
