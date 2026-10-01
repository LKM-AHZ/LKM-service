from typing import Any

from sqlalchemy import select

from core import messaging, worker_dlq
from core.config import settings
from core.db.dlq import DlqMessage
from core.db.outbox import OUTBOX_PENDING, OUTBOX_PUBLISHED, OutboxMessage


def test_make_model_maps_payload() -> None:
    """死信消息（payload dict + properties 还原 meta）映射为 DlqMessage。"""
    payload = {"fn": "send_code", "args": ["email", "a@b.com", "123456"]}
    m = worker_dlq._make_model(
        routing_key="event.send_code",
        payload=payload,
        attempts=2,
        reason="dead-lettered",
        status="pending",
        topic=messaging.TOPIC_DLQ,
    )
    assert m.routing_key == "event.send_code"
    assert m.payload_json["payload"] == payload
    assert m.attempts == 2
    assert m.status == "pending"
    assert m.exchange == messaging.TOPIC_DLQ


async def test_persist_writes_row(db: Any) -> None:
    """_persist 把模型落库到 db 表。"""
    m = worker_dlq._make_model(
        routing_key="event.send_code",
        payload={"fn": "send_code", "args": ["e", "c", "123"]},
        attempts=1,
        reason="dead-lettered",
        status="pending",
        topic=messaging.TOPIC_DLQ,
    )
    db.add(m)
    await db.commit()
    fetched = (await db.execute(select(DlqMessage))).scalars().one()
    assert fetched.routing_key == "event.send_code"


async def test_requeue_enqueues_and_marks_requeued(db: Any, monkeypatch: Any) -> None:
    """重放与状态变更同事务提交，relay 稍后投递。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://test:6650")

    m = DlqMessage(
        routing_key=messaging.RKEY_POINTS,
        payload_json={
            "payload": {
                "fn": "apply_point_event",
                "args": ["01890000-0000-7000-8000-000000000001", "like", "x:1"],
            }
        },
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)

    ok = await worker_dlq.requeue(db, m.id)
    assert ok is True
    await db.refresh(m)
    assert m.status == "requeued"
    assert m.requeued_at is not None
    queued = (await db.execute(select(OutboxMessage))).scalars().one()
    assert queued.routing_key == messaging.RKEY_POINTS
    assert queued.payload_json["fn"] == "apply_point_event"
    assert queued.event_id == m.id.hex


async def test_requeue_after_original_outbox_was_published(
    db: Any, monkeypatch: Any
) -> None:
    """原投递行保留期内仍是 published；死信重放须生成新的 pending 行。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://test:6650")
    payload = {
        "fn": "apply_point_event",
        "args": ["01890000-0000-7000-8000-000000000001", "post", "item:9"],
        "event_id": "dead-letter-1",
    }
    db.add(
        OutboxMessage(
            event_id="dead-letter-1",
            routing_key=messaging.RKEY_POINTS,
            payload_json=payload,
            status=OUTBOX_PUBLISHED,
        )
    )
    dead = DlqMessage(
        routing_key=messaging.RKEY_POINTS,
        payload_json={"payload": payload},
    )
    db.add(dead)
    await db.commit()
    await db.refresh(dead)

    assert await worker_dlq.requeue(db, dead.id) is True
    rows = (await db.execute(select(OutboxMessage))).scalars().all()
    assert len(rows) == 2
    assert {row.status for row in rows} == {OUTBOX_PENDING, OUTBOX_PUBLISHED}
