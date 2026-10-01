"""受控的大表 DDL：短锁加列、分批回填、并发建索引、分阶段收紧非空。

默认只打印计划；显式 ``--apply`` 才连接业务库执行。只允许已审计的大表名，
回填表达式仍是 DBA 审核的 SQL 表达式（可引用别名 t），不可接收 Web 用户输入。
每批单独提交，失败或中断后可重跑；参见父仓库 DEPLOYMENT.md。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
from contextlib import closing

import psycopg2
from psycopg2.extensions import connection

from core.config import settings
from core.secrets import reveal

LARGE_TABLES = (
    "content_items",
    "content_comments",
    "outbox_events",
    "outbox_archived",
    "interaction_view_logs",
    "points_ledger",
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_COLUMN_TYPES = ("text", "integer", "bigint", "boolean", "uuid", "timestamptz")
# Timescale hypertable 的主键必须含分区列；其余表以 UUID 主键推进。
_COMPOSITE_KEYS = frozenset({"outbox_events", "outbox_archived", "points_ledger"})


def _ident(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value) or len(value) > 63:
        raise ValueError(f"非法 PostgreSQL 标识符: {value!r}")
    return f'"{value}"'


def _expression(value: str) -> str:
    # 防止计划文件变成多语句脚本；表达式的语义仍须人工审核。
    if not value.strip() or any(
        token in value for token in (";", "--", "/*", "*/", "$$")
    ):
        raise ValueError("回填表达式必须是单个 SQL 表达式，且不能含注释或分号")
    return value


def _table(args: argparse.Namespace) -> str:
    return f"{_ident(args.schema)}.{_ident(args.table)}"


def _not_null_constraint(args: argparse.Namespace) -> str:
    name = f"ck_{args.table}_{args.column}_not_null"
    if len(name) > 63:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = f"{name[:54]}_{digest}"
    return name


def _connect() -> connection:
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=reveal(settings.db_password),
        connect_timeout=10,
        application_name="lkm-online-ddl",
    )


def _short_ddl(conn: connection, statement: str) -> None:
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _plan(args: argparse.Namespace) -> str:
    table = _table(args)
    if args.command == "add-column":
        column = _ident(args.column)
        return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {args.type}"
    if args.command == "backfill":
        column = _ident(args.column)
        expression = _expression(args.expression)
        if args.table in _COMPOSITE_KEYS:
            keys = "t.created_at, t.id"
            cursor_filter = (
                "(%s::timestamptz IS NULL OR "
                "(t.created_at, t.id) > (%s::timestamptz, %s::uuid))"
            )
            join = "t.created_at = batch.created_at AND t.id = batch.id"
            returning = "batch.created_at, batch.id"
        else:
            keys = "t.id"
            cursor_filter = "(%s::uuid IS NULL OR t.id > %s::uuid)"
            join = "t.id = batch.id"
            returning = "batch.id"
        return (
            f"WITH batch AS (SELECT {keys} FROM {table} AS t "
            f"WHERE t.{column} IS NULL AND ({expression}) IS NOT NULL "
            f"AND {cursor_filter} ORDER BY {keys} LIMIT %s FOR UPDATE SKIP LOCKED) "
            f"UPDATE {table} AS t SET {column} = ({expression}) "
            f"FROM batch WHERE {join} RETURNING {returning}"
        )
    if args.command == "index":
        columns = ", ".join(_ident(c) for c in args.columns)
        unique = "UNIQUE " if args.unique else ""
        return (
            f"CREATE {unique}INDEX CONCURRENTLY {_ident(args.name)} "
            f"ON {table} USING {args.method} ({columns})"
        )
    if args.command == "set-not-null":
        column = _ident(args.column)
        constraint = _ident(_not_null_constraint(args))
        return "\n".join(
            (
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                f"CHECK ({column} IS NOT NULL) NOT VALID",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}",
                f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL",
                f"ALTER TABLE {table} DROP CONSTRAINT {constraint}",
            )
        )
    raise AssertionError(args.command)


def _backfill(conn: connection, args: argparse.Namespace, query: str) -> None:
    total = 0
    composite = args.table in _COMPOSITE_KEYS
    last_created: object = None
    last_id: object = None
    for _ in range(args.max_batches):
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                params = (
                    (last_created, last_created, last_id, args.batch_size)
                    if composite
                    else (last_id, last_id, args.batch_size)
                )
                cursor.execute(query, params)
                rows = cursor.fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        count = len(rows)
        total += count
        if not rows:
            break
        last_key = max(rows)
        if composite:
            last_created, last_id = last_key
        else:
            last_id = last_key[0]
        print(f"本批 {count} 行，累计 {total} 行")
        time.sleep(args.sleep_ms / 1000)

    # 检查完整性：SKIP LOCKED 可能造成某批暂时为 0，表达式也可能无法填满所有行。
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT EXISTS (SELECT 1 FROM {_table(args)} "
            f"WHERE {_ident(args.column)} IS NULL)"
        )
        remaining = cursor.fetchone()[0]
    conn.commit()
    if remaining:
        raise RuntimeError(
            "仍有 NULL 行；可能达到批次上限、存在并发锁或表达式产生 NULL，请重跑"
        )
    print(f"回填完成，共 {total} 行")


def _index(conn: connection, args: argparse.Namespace, query: str) -> None:
    conn.autocommit = True  # CONCURRENTLY 与 Timescale 分 chunk 建索引都在事务外执行
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT i.indisvalid FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE n.nspname = %s AND c.relname = %s",
            (args.schema, args.name),
        )
        existing = cursor.fetchone()
        if existing is not None:
            status = "有效" if existing[0] else "无效"
            raise RuntimeError(
                f"索引 {args.name} 已存在（{status}）；请核对定义后再继续"
            )
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
        )
        if cursor.fetchone()[0]:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema = %s AND hypertable_name = %s)",
                (args.schema, args.table),
            )
            if cursor.fetchone()[0]:
                if args.unique:
                    raise RuntimeError(
                        "Timescale hypertable 不支持按 chunk 建唯一索引；须人工评审"
                    )
                query = query.replace("INDEX CONCURRENTLY", "INDEX", 1)
                query += " WITH (timescaledb.transaction_per_chunk)"
        cursor.execute("SET lock_timeout = '2s'")
        cursor.execute("SET statement_timeout = '30min'")
        print(query)
        cursor.execute(query)


def _set_not_null(conn: connection, args: argparse.Namespace) -> None:
    table = _table(args)
    column = _ident(args.column)
    constraint_name = _not_null_constraint(args)
    constraint = _ident(constraint_name)
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT a.attnotnull FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) AND a.attname = %s",
            (f"{table}", args.column),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"列 {table}.{args.column} 不存在")
        cursor.execute(
            "SELECT convalidated FROM pg_constraint WHERE conrelid = to_regclass(%s) "
            "AND conname = %s",
            (table, constraint_name),
        )
        existing = cursor.fetchone()
    conn.commit()
    if row[0]:
        if existing is not None:
            _short_ddl(conn, f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        print("列已是 NOT NULL")
        return
    if existing is None:
        _short_ddl(
            conn,
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            f"CHECK ({column} IS NOT NULL) NOT VALID",
        )
    if existing is None or not existing[0]:
        # VALIDATE 只取 SHARE UPDATE EXCLUSIVE，允许正常读写；长扫描独立事务。
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                cursor.execute("SET LOCAL statement_timeout = '30min'")
                cursor.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _short_ddl(conn, f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
    _short_ddl(conn, f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="public")
    parser.add_argument("--apply", action="store_true", help="实际执行；默认仅打印计划")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add-column", "backfill", "index", "set-not-null"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--table", choices=LARGE_TABLES, required=True)
        if name != "index":
            cmd.add_argument("--column", required=True)
        if name == "add-column":
            cmd.add_argument("--type", choices=_COLUMN_TYPES, required=True)
        elif name == "backfill":
            cmd.add_argument("--expression", required=True)
            cmd.add_argument("--batch-size", type=int, default=500)
            cmd.add_argument("--max-batches", type=int, default=1000)
            cmd.add_argument("--sleep-ms", type=int, default=50)
        elif name == "index":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--columns", nargs="+", required=True)
            cmd.add_argument(
                "--method", choices=("btree", "gin", "gist", "brin"), default="btree"
            )
            cmd.add_argument("--unique", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "backfill" and (
        args.batch_size < 1 or args.max_batches < 1 or args.sleep_ms < 0
    ):
        parser.error("batch-size/max-batches 须为正数，sleep-ms 须非负")
    query = _plan(args)
    if not args.apply:
        print(query)
        if args.command == "index" and args.table in _COMPOSITE_KEYS:
            print("若数据库已转 Timescale hypertable，执行时改用 transaction_per_chunk")
        return 0
    with closing(_connect()) as conn:
        if args.command == "add-column":
            _short_ddl(conn, query)
        elif args.command == "backfill":
            _backfill(conn, args, query)
        elif args.command == "index":
            _index(conn, args, query)
        else:
            _set_not_null(conn, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
