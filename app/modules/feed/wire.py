"""timeline/feed 读热响应的 msgspec Struct 镜像（roadmap §6.5.2，M5）。

Pydantic ``FeedResponse`` 仍负责构造/校验；本模块只做「校验后 → 可 msgspec 直编」的镜像转换。
字段名与 Pydantic 逐一对齐且保持 **snake_case**：现有前端契约
（``LKM-official-website/src/lib/api/modules/timeline.ts``）消费 ``item_type``/``content_preview``/
``next_cursor``，故**不按蓝图文面用 to_camel**（偏离已登记路线图 §8）。

编码等价性：msgspec 对 ``datetime``(UTC→``...Z``/naive/带偏移)、float、None、unicode 的输出与
``model_dump(mode="json")`` 实测 JSON 等价，转换无需预处理；由 ``tests/test_read_msgspec.py`` 守。
"""

from __future__ import annotations

import datetime
import uuid

import msgspec

from app.modules.feed.schemas import FeedResponse


class FeedItemWire(msgspec.Struct):
    """``FeedItem`` 的 msgspec 镜像（字段同序同名）。"""

    item_type: str
    # 与 schemas.FeedItem 保持一致：三个 id 都是 uuid.UUID（镜像的契约就是逐字段对齐）
    id: uuid.UUID
    author_id: uuid.UUID | None
    author_name: str
    title: str
    content_preview: str
    created_at: datetime.datetime
    sort_score: float
    board_id: uuid.UUID | None
    url: str


class FeedResponseWire(msgspec.Struct):
    """``FeedResponse`` 的 msgspec 镜像。"""

    items: list[FeedItemWire]
    next_cursor: str | None


def to_wire(resp: FeedResponse) -> FeedResponseWire:
    """Pydantic ``FeedResponse``（已校验）→ msgspec 镜像；显式取字段，避免 model_dump 开销。"""
    return FeedResponseWire(
        items=[
            FeedItemWire(
                item_type=i.item_type,
                id=i.id,
                author_id=i.author_id,
                author_name=i.author_name,
                title=i.title,
                content_preview=i.content_preview,
                created_at=i.created_at,
                sort_score=i.sort_score,
                board_id=i.board_id,
                url=i.url,
            )
            for i in resp.items
        ],
        next_cursor=resp.next_cursor,
    )
