"""Auth audit hypertable schema and optional TimescaleDB setup."""

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
