"""event_failures 的人工重放通道（蓝图 §5.1 第 3 条）。

该条要求「`event_failures` 表 + 告警驱动人工/脚本修复后重放」；此前只有只读的 ClickHouse
分析入口，运维修好根因后没有可执行的重放手段。本文件验重放的三条硬性质：
① 重新入队 outbox（沿用原 event_id）并摘除归档行；② 总线未启用时**不删归档行**（防丢唯一
审计副本）；③ 行不存在时不误报成功。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import event_failure as ef_mod
from app.db.event_failure import EventFailure, replay_failure
from app.db.outbox import OutboxMessage


async def _add_failure(db: AsyncSession, **kw: Any) -> EventFailure:
    row = EventFailure(
        event_id=str(kw.get("event_id", "eid-1")),
        routing_key=str(kw.get("routing_key", "event.apply_point")),
        payload_json=kw.get("payload_json", {"fn": "apply_point_event", "args": [1]}),
        attempt_count=int(kw.get("attempt_count", 5)),
        reason=str(kw.get("reason", "relay exhausted: max tries reached")),
    )
    db.add(row)
    await db.flush()
    return row


class TestReplayFailure:
    async def should_requeue_and_drop_archive_row(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        async def _enqueue(
            session: AsyncSession,
            routing_key: str,
            payload: dict[str, Any],
            *,
            event_id: str | None = None,
        ) -> bool:
            calls.append(
                {"routing_key": routing_key, "payload": payload, "event_id": event_id}
            )
            session.add(
                OutboxMessage(
                    event_id=event_id or "x",
                    routing_key=routing_key,
                    payload_json=payload,
                )
            )
            return True

        monkeypatch.setattr(ef_mod, "enqueue_outbox", _enqueue)
        monkeypatch.setattr(ef_mod.settings, "pulsar_url", "pulsar://test:6650")

        row = await _add_failure(db)
        failure_id = row.id

        assert await replay_failure(db, failure_id) is True
        assert len(calls) == 1
        # 沿用原 event_id / routing_key（下游按 event_id 幂等去重）
        assert calls[0]["event_id"] == "eid-1"
        assert calls[0]["routing_key"] == "event.apply_point"
        assert calls[0]["payload"] == {"fn": "apply_point_event", "args": [1]}
        # 归档行已摘除
        assert await db.scalar(select(EventFailure).where(EventFailure.id == failure_id)) is None

    async def should_keep_archive_row_when_bus_disabled(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ef_mod.settings, "pulsar_url", "")

        row = await _add_failure(db, event_id="eid-2")
        assert await replay_failure(db, row.id) is False
        # 投不出去时绝不能删掉唯一的审计副本
        assert await db.scalar(select(EventFailure).where(EventFailure.id == row.id)) is not None

    async def should_return_false_for_unknown_id(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ef_mod.settings, "pulsar_url", "pulsar://test:6650")
        import uuid

        assert await replay_failure(db, uuid.uuid4()) is False
