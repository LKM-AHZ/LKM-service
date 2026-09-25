"""notification 列表读响应的 msgspec 镜像（B6c，roadmap §6.5.2 扩面）。

``payload`` 是 DB JSONB 读出的 dict，内容为 JSON 原生值，msgspec 可直接编码（与
``model_dump(mode="json")`` 等价，由 ``tests/test_read_msgspec.py`` 守）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import msgspec

from app.core.common import PageData
from app.modules.notification.schemas import NotificationOut


class NotificationWire(msgspec.Struct):
    """``NotificationOut`` 的 msgspec 镜像（字段同序同名）。"""

    id: uuid.UUID
    type: str
    actor_id: uuid.UUID | None
    target_id: uuid.UUID | None
    payload: dict[str, Any]
    read_at: datetime.datetime | None
    created_at: datetime.datetime


class NotificationPageWire(msgspec.Struct):
    items: list[NotificationWire]
    total: int
    page: int
    pages: int


def to_wire(page: PageData[NotificationOut]) -> NotificationPageWire:
    return NotificationPageWire(
        items=[
            NotificationWire(
                id=item.id,
                type=item.type,
                actor_id=item.actor_id,
                target_id=item.target_id,
                payload=item.payload,
                read_at=item.read_at,
                created_at=item.created_at,
            )
            for item in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )
