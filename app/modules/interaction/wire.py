"""interaction 列表读响应的 msgspec 镜像（B6c，roadmap §6.5.2 扩面）。

收藏与历史共用内容摘要字段（content_id/content_type/title/slug/board_id），各自多一个
时间字段（``created_at`` / ``viewed_at``）。Pydantic 仍负责构造/校验，本模块只做镜像转换；
字段名保持 snake_case（与 ``feed/wire.py`` 同理）。
"""

from __future__ import annotations

import datetime
import uuid

import msgspec

from app.core.common import PageData
from app.modules.interaction.schemas import FavoriteItem, HistoryItem


class FavoriteItemWire(msgspec.Struct):
    """``FavoriteItem`` 的 msgspec 镜像。"""

    content_id: uuid.UUID
    content_type: str
    title: str
    slug: str | None
    board_id: uuid.UUID
    created_at: datetime.datetime


class FavoritePageWire(msgspec.Struct):
    items: list[FavoriteItemWire]
    total: int
    page: int
    pages: int


class HistoryItemWire(msgspec.Struct):
    """``HistoryItem`` 的 msgspec 镜像。"""

    content_id: uuid.UUID
    content_type: str
    title: str
    slug: str | None
    board_id: uuid.UUID
    viewed_at: datetime.datetime


class HistoryPageWire(msgspec.Struct):
    items: list[HistoryItemWire]
    total: int
    page: int
    pages: int


def favorites_to_wire(page: PageData[FavoriteItem]) -> FavoritePageWire:
    return FavoritePageWire(
        items=[
            FavoriteItemWire(
                content_id=item.content_id,
                content_type=item.content_type,
                title=item.title,
                slug=item.slug,
                board_id=item.board_id,
                created_at=item.created_at,
            )
            for item in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )


def history_to_wire(page: PageData[HistoryItem]) -> HistoryPageWire:
    return HistoryPageWire(
        items=[
            HistoryItemWire(
                content_id=item.content_id,
                content_type=item.content_type,
                title=item.title,
                slug=item.slug,
                board_id=item.board_id,
                viewed_at=item.viewed_at,
            )
            for item in page.items
        ],
        total=page.total,
        page=page.page,
        pages=page.pages,
    )
