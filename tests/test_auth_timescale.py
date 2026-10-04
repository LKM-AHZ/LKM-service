"""Auth audit hypertable schema and optional TimescaleDB setup."""

import importlib
from unittest.mock import AsyncMock

from sqlalchemy.exc import ProgrammingError

from auth.db.init import _ensure_audit_hypertable
from auth.models import AuditLog


def test_audit_primary_key_contains_time_partition() -> None:
    assert [column.name for column in AuditLog.__table__.primary_key] == [
        "created_at",
        "id",
    ]


async def test_auth_timescale_setup_runs_in_order() -> None:
    conn = AsyncMock()
    await _ensure_audit_hypertable(conn)

    sql = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert sql[0] == "CREATE EXTENSION IF NOT EXISTS timescaledb"
    assert "create_hypertable('audit_logs', 'created_at'" in sql[1]
    assert conn.begin_nested.await_count == 2


async def test_auth_timescale_missing_extension_keeps_plain_table() -> None:
    conn = AsyncMock()
    conn.execute.side_effect = ProgrammingError(
        "CREATE EXTENSION", {}, Exception("extension unavailable")
    )
    await _ensure_audit_hypertable(conn)

    assert conn.execute.await_count == 1
    conn.begin_nested.return_value.rollback.assert_awaited_once()


def test_auth_migration_rewrites_legacy_primary_key(monkeypatch) -> None:
    revision = importlib.import_module("alembic_auth.versions.0004_audit_hypertable")
    statements: list[str] = []

    class _Bind:
        def scalar(self, _sql: object) -> str:
            return "PRIMARY KEY (id)"

    monkeypatch.setattr(revision.op, "get_bind", _Bind)
    monkeypatch.setattr(revision.op, "execute", statements.append)
    revision.upgrade()

    assert statements[:2] == [
        "ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_pkey",
        "ALTER TABLE audit_logs ADD PRIMARY KEY (created_at, id)",
    ]
    assert "migrate_data => TRUE" in statements[2]
