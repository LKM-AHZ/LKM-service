"""interaction 域模型：收藏 + 浏览记录 + 关注关系（M6.6 / M2）。

**关注关系归本域**：``UserFollow``(用户关注) 与 ``BoardFollow``(版块关注) 原属 feed 域，
现迁入 interaction——蓝图 §3.1 与 §7.2 的**模块目标形态**都把 interaction 定义为「收窄：
收藏、关注、浏览记录」，迁入后代码与该目标逐字一致；信息流域（feed）只保留时间线生成。

三张表各自的幂等取法：
- ``interaction_favorites`` 复合主键 ``(content_id, user_id)`` —— 天然保证「同一用户对同一
  内容最多一条」（收藏幂等），与 ``content_likes`` 同款取法；
- ``interaction_view_logs`` 代理主键 + ``(user_id, content_id)`` 唯一约束 —— 重复上报同内容
  走 upsert 只刷新 ``viewed_at``，行数上界 = 用户数 × 内容数（保留策略见 tasks.py）；
- ``user_follows`` / ``board_follows`` —— **软删墓碑**：唯一约束只看 (follower, target)，
  取消关注只置 ``deleted_at`` 不删行，故「重新关注」是复活同一行而非插新行。

``user_id`` 一律裸 UUID（S5 拆库后 auth 库才是用户权威，业务库不做物理外键）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)

if TYPE_CHECKING:
    from app.modules.content.models import Board


class InteractionFavorite(Base):
    """内容收藏记录（幂等由复合主键保证，不依赖应用层查重）。"""

    __tablename__: str = "interaction_favorites"
    __table_args__: tuple[Any, ...] = (
        # 「我的收藏」按 user 倒序翻页
        Index("ix_interaction_fav_user_created", "user_id", "created_at"),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class InteractionViewLog(UUIDPrimaryKeyMixin, Base):
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
        # content_id 反查：本表 content_id 带 ON DELETE CASCADE，而唯一约束
        # (user_id, content_id) 与上面两个索引都不是 content_id 打头，内容行硬删时 PG 只能
        # 顺序扫本表找引用行（对比 interaction_favorites 的复合主键就是 content_id 打头）。
        Index("ix_interaction_view_content", "content_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False
    )
    viewed_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class UserFollow(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """用户关注关系（软删墓碑）：follower 关注 following。

    唯一约束针对``(follower_id, following_id)``——软删行保留以便幂等重关注；
    活动关注统一 ``deleted_at IS NULL``。反向查「谁关注了我」走 following_id 索引。
    """

    __tablename__: str = "user_follows"
    __table_args__: tuple[UniqueConstraint, Index, Index] = (
        UniqueConstraint("follower_id", "following_id", name="uq_user_follows_pair"),
        Index("ix_user_follows_following_created", "following_id", "created_at"),
        # "我关注了谁"（follower 视角）按时间排序/分页
        Index("ix_user_follows_follower_created", "follower_id", "created_at"),
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    following_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class BoardFollow(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """用户关注版块关系（软删墓碑）：follower 关注 board_id。"""

    __tablename__: str = "board_follows"
    __table_args__: tuple[UniqueConstraint, Index] = (
        UniqueConstraint("follower_id", "board_id", name="uq_board_follows_pair"),
        Index("ix_board_follows_board", "board_id"),
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("boards.id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    board: Mapped[Board] = relationship(back_populates="followers")
