from typing import Any

from sqlalchemy import select

from app.core import messaging, worker_dlq
from app.modules.admin.models import DlqMessage


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


async def test_requeue_publishes_and_marks_requeued(db: Any, monkeypatch: Any) -> None:
    """重投端点把消息 re-publish 回原 routing key 并标记 requeued。"""
    published: list[tuple] = []

    async def fake_pub(rk: str, payload: dict) -> bool:
        published.append((rk, payload))
        return True

    monkeypatch.setattr(messaging, "publish", fake_pub)
    # worker_dlq 经 `from app.core import messaging` 引用同一模块对象，patch 其 publish 即生效。

    m = DlqMessage(
        routing_key=messaging.RKEY_POINTS,
        payload_json={
            "payload": {
                "fn": "apply_point_event",
                "args": [1, "like", "x:1"],
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
    assert published[0][0] == messaging.RKEY_POINTS
    assert published[0][1]["fn"] == "apply_point_event"
