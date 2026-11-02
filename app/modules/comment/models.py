"""评论（comment）域 ORM 模型。

步骤 9 切片 2 自 ``content.models`` 迁出，**表名、列、索引一字不变**，无需迁移。
``content_comments.content_id`` 仍是指向 ``content_items.id`` 的字符串外键（content 侧
不 import 本模块的类），跨域的 ``content_item`` / ``like_records`` 关系已随之删除。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import (  # 注意是 db.base 不是 db.models
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class ContentComment(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """统一内容评论（对齐原 forum_comments 的完整模式：floor_number/parent_id/like_count）。

    软删（批 4）：楼层号生成取**含已软删**的最大值 +1，否则可见楼层会重号；
    计数对账（``comment/counters.py``）与在线计数同用「未软删」口径。
    """

    __tablename__: str = "content_comments"
    __table_args__: tuple[Any, ...] = (
        Index("ix_content_comments_item_floor", "content_id", "floor_number"),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_items.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    floor_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("content_comments.id"), nullable=True
    )
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    # remote_side 用字符串：主键 id 来自 UUIDPrimaryKeyMixin，类体内无 `id` 名字绑定
    # （写 [id] 会解析到内置函数 id，configure_mappers 直接报错）
    parent: Mapped[ContentComment | None] = relationship(
        remote_side="ContentComment.id", back_populates="replies"
    )
    replies: Mapped[list[ContentComment]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class ContentCommentLike(Base):
    """评论点赞明细（``content_comments.like_count`` 的真相源）。"""

    __tablename__: str = "content_comment_likes"

    comment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_comments.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
