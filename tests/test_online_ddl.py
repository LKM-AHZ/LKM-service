"""大表运维工具的 SQL 安全边界与分批语义（不连接生产库）。"""

from __future__ import annotations

import argparse

import pytest

from scripts import online_ddl


def test_backfill_plan_uses_composite_key_on_partitioned_tables() -> None:
    args = argparse.Namespace(
        command="backfill",
        schema="public",
        table="outbox_events",
        column="new_value",
        expression="t.attempt_count + 1",
    )
    query = online_ddl._plan(args)
    assert "SELECT t.created_at, t.id" in query
    assert "t.created_at = batch.created_at AND t.id = batch.id" in query
    assert "(t.created_at, t.id) > (%s::timestamptz, %s::uuid)" in query
    assert "LIMIT %s FOR UPDATE SKIP LOCKED" in query


def test_rejects_unreviewable_multistatement_expression() -> None:
    args = argparse.Namespace(
        command="backfill",
        schema="public",
        table="content_items",
        column="new_value",
        expression="1; DROP TABLE users",
    )
    with pytest.raises(ValueError, match="单个 SQL 表达式"):
        online_ddl._plan(args)


def test_index_plan_is_concurrent_and_quotes_identifiers() -> None:
    args = argparse.Namespace(
        command="index",
        schema="public",
        table="content_items",
        name="ix_content_new_value",
        columns=["new_value", "created_at"],
        method="btree",
        unique=False,
    )
    assert online_ddl._plan(args) == (
        'CREATE INDEX CONCURRENTLY "ix_content_new_value" '
        'ON "public"."content_items" USING btree ("new_value", "created_at")'
    )


def test_backfill_commits_each_batch_and_checks_completion(monkeypatch) -> None:
    class Cursor:
        def __init__(self) -> None:
            self.rowcount = 0
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if query.startswith("WITH batch"):
                self.calls += 1

        def fetchall(self):
            return [[(1,), (2,)], [(3,)], []][self.calls - 1]

        def fetchone(self):
            return (False,)

    class Connection:
        def __init__(self) -> None:
            self.c = Cursor()
            self.commits = 0

        def cursor(self):
            return self.c

        def commit(self):
            self.commits += 1

        def rollback(self):
            raise AssertionError("unexpected rollback")

    conn = Connection()
    monkeypatch.setattr(online_ddl.time, "sleep", lambda _seconds: None)
    args = argparse.Namespace(
        schema="public",
        table="content_items",
        column="new_value",
        batch_size=2,
        max_batches=5,
        sleep_ms=0,
    )
    online_ddl._backfill(conn, args, "WITH batch ...")
    assert conn.c.calls == 3
    assert conn.commits == 4  # 3 updates + final completeness query


def test_hypertable_index_uses_transaction_per_chunk() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.answers = iter((None, (True,), (True,)))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            self.queries.append(query)

        def fetchone(self):
            return next(self.answers)

    class Connection:
        def __init__(self) -> None:
            self.autocommit = False
            self.c = Cursor()

        def cursor(self):
            return self.c

    conn = Connection()
    args = argparse.Namespace(
        schema="public",
        table="outbox_events",
        name="ix_outbox_new",
        unique=False,
    )
    online_ddl._index(
        conn, args, "CREATE INDEX CONCURRENTLY ix_outbox_new ON outbox_events (id)"
    )
    assert conn.autocommit is True
    assert conn.c.queries[-1].endswith("WITH (timescaledb.transaction_per_chunk)")
    assert "CONCURRENTLY" not in conn.c.queries[-1]
