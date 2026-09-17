"""interaction 域模型：收藏与浏览记录（M6.6）。

两张表都锚定 ``content_items.id``（全库 ``content_id`` 的统一所指）：
- ``interaction_favorites`` 复合主键 ``(content_id, user_id)`` —— 天然保证「同一用户对同一
  内容最多一条」（收藏幂等），与 ``content_likes`` 同款取法；
- ``interaction_view_logs`` 代理主键 + ``(user_id, content_id)`` 唯一约束 —— 重复上报同内容
  走 upsert 只刷新 ``viewed_at``，行数上界 = 用户数 × 内容数（保留策略见 tasks.py）。

``user_id`` 一律裸 Integer（S5 拆库后 auth 库才是用户权威，业务库不做物理外键）。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, now_iso


class InteractionFavorite(Base):
    """内容收藏记录（幂等由复合主键保证，不依赖应用层查重）。"""

    __tablename__: str = "interaction_favorites"
    __table_args__: tuple[Any, ...] = (
        # 「我的收藏」按 user 倒序翻页
        Index("ix_interaction_fav_user_created", "user_id", "created_at"),
    )

    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class InteractionViewLog(Base):
    """浏览记录：``(user_id, content_id)`` 唯一，重复上报只推进 ``viewed_at``。"""

    __tablename__: str = "interaction_view_logs"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint(
            "user_id", "content_id", name="uq_interaction_view_user_content"
        ),
        # 「我的浏览历史」按 viewed_at 倒序翻页
        Index("ix_interaction_view_user_viewed", "user_id", "viewed_at"),
        # 保留策略清理任务按 viewed_at 扫描过期行
        Index("ix_interaction_view_viewed", "viewed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False
    )
    viewed_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
