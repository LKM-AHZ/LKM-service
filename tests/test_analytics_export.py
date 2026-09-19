"""分析导出 owner 侧测试（M5 7.2.6 路 A）。

用 fake CH client + 真实 PG schema（conftest db/auth_db）：验证批量不逐行（窗口分批、
插入批次数恒定）、CH 侧 max(id) 水位增量、重跑 diff=0（幂等）、失败不被静默吞、可空列落空串。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.event_failure import EventFailure
from app.db.event_failure_export import CH_TABLE as FAILURES_TABLE
from app.db.event_failure_export import export_event_failures
from app.modules.auth.audit_export import CH_TABLE as AUDITS_TABLE
from app.modules.auth.audit_export import export_audit_logs
from app.modules.auth.models import AuditLog
from tests.fakes import FakeClickHouseClient


async def _seed_failures(db: AsyncSession, n: int) -> None:
    db.add_all(
        [
            EventFailure(
                event_id=f"evt-{i}",
                routing_key="event.notify_upload",
                payload_json={"fn": "x", "args": [i]},
                attempt_count=5,
                reason="relay exhausted",
            )
            for i in range(n)
        ]
    )
    await db.commit()


async def should_export_in_batches_without_n_plus_1(db: AsyncSession) -> None:
    await _seed_failures(db, 5)
    client = FakeClickHouseClient()

    total = await export_event_failures(db, client, window=2)

    assert total == 5
    assert client.inserted_rows() == 5
    # 5 行 / 窗口 2 → 3 次批量 insert（2+2+1），绝不逐行
    assert len(client.inserts) == 3
    # CH 侧命令仅水位查询 1 次 + insert 3 次（query 列表不含 insert）
    assert len(client.queries) == 1


async def should_be_idempotent_on_rerun(db: AsyncSession) -> None:
    await _seed_failures(db, 4)
    client = FakeClickHouseClient()

    first = await export_event_failures(db, client, window=10)
    second = await export_event_failures(db, client, window=10)

    assert first == 4
    assert second == 0  # 水位已推进，重跑无新行
    assert client.watermarks[FAILURES_TABLE] == client.inserts[0][1][-1][0]


async def should_respect_persisted_watermark(db: AsyncSession) -> None:
    await _seed_failures(db, 3)
    # uuid7 主键：预置「中间一行」的字符串 id 作水位，只剩最后一行待导
    # （字典序即时间序；用真实 id 而非硬编码，避免依赖生成时刻）。
    ids = (
        (await db.execute(select(EventFailure.id).order_by(EventFailure.id)))
        .scalars()
        .all()
    )
    client = FakeClickHouseClient(watermarks={FAILURES_TABLE: str(ids[1])})

    total = await export_event_failures(db, client, window=10)

    assert total == 1
    assert client.inserted_rows() == 1
    assert client.inserts[0][1][0][0] == str(ids[2])


async def should_treat_blank_ch_watermark_as_no_watermark(db: AsyncSession) -> None:
    """CH 空表的水位是**空串**而非 NULL——必须按「无水位」走全量。

    CH 的 ``max()`` 在空集上返回该类型的零值：``String`` 列即空串（整数列时代是 0，
    恰好与「无水位」等价，故只判 None 的旧实现也没事）。``id`` 改 String 后若把空串
    当水位，PG 侧会生成 ``id > ''`` 直接抛 uuid 解析错——真机上表现为「CH 空表首次
    导出必然失败」。
    """
    await _seed_failures(db, 2)
    client = FakeClickHouseClient(watermarks={FAILURES_TABLE: ""})

    total = await export_event_failures(db, client, window=10)

    assert total == 2  # 空串水位 → 视为首次全量
    assert client.inserted_rows() == 2


async def should_raise_on_ch_failure(db: AsyncSession) -> None:
    await _seed_failures(db, 2)
    client = FakeClickHouseClient(fail_insert=True)

    with pytest.raises(RuntimeError, match="insert failure"):
        await export_event_failures(db, client, window=10)


async def should_export_audit_logs_with_empty_text_columns(
    auth_db: AsyncSession,
) -> None:
    logs = [
        AuditLog(action="login_fail", detail=None, ip_address=None),
        AuditLog(action="permission_change", detail="granted", ip_address="1.2.3.4"),
    ]
    auth_db.add_all(logs)
    await auth_db.commit()
    client = FakeClickHouseClient()

    total = await export_audit_logs(auth_db, client, window=10)

    assert total == 2
    assert client.inserted_rows() == 2
    _, data, cols = client.inserts[0]
    assert data[0][cols.index("detail")] == ""
    assert data[0][cols.index("ip_address")] == ""
    # uuid 主键/用户列以字符串形式落 CH String 列（uuid 对象不能直接进 String 列）
    assert data[0][cols.index("id")] == str(min(logs, key=lambda r: r.id).id)
    assert data[0][cols.index("user_id")] is None  # 未关联用户 → Nullable(String) 落 NULL
    # 水位推进到本批最大 id（字符串形式，CH 侧 max(id) 下一次读回同值）
    assert client.watermarks[AUDITS_TABLE] == str(max(logs, key=lambda r: r.id).id)


async def should_audit_export_be_idempotent(auth_db: AsyncSession) -> None:
    auth_db.add_all([AuditLog(action="logout")])
    await auth_db.commit()
    client = FakeClickHouseClient()

    assert await export_audit_logs(auth_db, client, window=10) == 1
    assert await export_audit_logs(auth_db, client, window=10) == 0
