"""event_failures 的人工重放通道（蓝图 §5.1 第 3 条）。

该条要求「`event_failures` 表 + 告警驱动人工/脚本修复后重放」；此前只有只读的 ClickHouse
分析入口，运维修好根因后没有可执行的重放手段。本文件验重放的三条硬性质：
① 重新入队 outbox（沿用原 event_id）并保留审计行；② 总线未启用时**不改归档行**（防丢唯一
审计副本）；③ 行不存在时不误报成功。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import event_failure as ef_mod
from core.db.event_failure import EventFailure, replay_failure
from core.db.outbox import OutboxMessage


async def _add_failure(db: AsyncSession, **kw: Any) -> EventFailure:
    row = EventFailure(
        event_id=str(kw.get("event_id", "eid-1")),
        routing_key=str(kw.get("routing_key", "event.apply_point")),
        payload_json=kw.get("payload_json", {
            "fn": "apply_point_event",
            "args": ["01890000-0000-7000-8000-000000000001", "post", "item:9"],
        }),
        attempt_count=int(kw.get("attempt_count", 5)),
        reason=str(kw.get("reason", "relay exhausted: max tries reached")),
    )
    db.add(row)
    await db.flush()
    return row


class TestReplayFailure:
    async def should_requeue_and_mark_archive_row(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        async def _enqueue(
            session: AsyncSession,
            routing_key: str,
            payload: dict[str, Any],
            *,
            event_id: str | None = None,
            replay: bool = False,
        ) -> bool:
            calls.append(
                {"routing_key": routing_key, "payload": payload, "event_id": event_id, "replay": replay}
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
        assert calls[0]["replay"] is True
        assert calls[0]["routing_key"] == "event.apply_point"
        assert calls[0]["payload"] == row.payload_json
        # 原失败记录保留，重复重放被拒绝。
        saved = await db.scalar(select(EventFailure).where(EventFailure.id == failure_id))
        assert saved is not None and saved.replayed_at is not None
        assert await replay_failure(db, failure_id) is False

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

    async def should_allow_corrected_payload_and_preserve_original(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ef_mod.settings, "pulsar_url", "pulsar://test:6650")
        row = await _add_failure(db, routing_key="unknown.key")
        assert await replay_failure(db, row.id) is False
        corrected = {
            "fn": "apply_point_event",
            "args": ["01890000-0000-7000-8000-000000000001", "post", "item:9"],
        }
        assert await replay_failure(
            db, row.id, routing_key="event.apply_point", payload=corrected
        ) is True
        await db.flush()
        queued = await db.scalar(
            select(OutboxMessage).where(OutboxMessage.event_id == row.event_id)
        )
        assert queued is not None and queued.payload_json == corrected
        assert row.routing_key == "unknown.key" and row.replayed_at is not None
