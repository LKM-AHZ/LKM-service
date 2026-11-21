"""interaction 域模型：收藏 + 浏览记录 + 关注关系 + 内容订阅/单帖 watch（M6.6 / M2）。

**关注/订阅归本域**：``UserFollow``(用户关注) 与版块关注原属 feed 域，现迁入 interaction；
版块关注已统一为 ``ContentSubscription(target_type='board')``，用户关注仍是独立的社交关系。
蓝图 §3.1 与 §7.2 把 interaction 定义为「收窄：收藏、关注、浏览记录」，本域在此之上承载
内容订阅与单帖 watch；信息流域（feed）只保留时间线生成。

各表幂等取法：
- ``interaction_favorites`` 复合主键 ``(content_id, user_id)`` —— 天然保证「同一用户对同一
  内容最多一条」（收藏幂等），与 ``content_likes`` 同款取法；
- ``interaction_view_logs`` 代理主键 + ``(user_id, content_id)`` 唯一约束 —— 重复上报同内容
  走 upsert 只刷新 ``viewed_at``，行数上界 = 用户数 × 内容数（保留策略见 tasks.py）；
- ``user_follows`` / ``content_subscriptions`` / ``post_watches`` —— **软删墓碑**：唯一约束
  只看 (user, target)，取消订阅/watch 只置 ``deleted_at`` 不删行，重新订阅是复活同一行。
- ``board_follows`` 已由迁移 0006 并入 ``content_subscriptions(target_type='board')``。

``user_id`` 一律裸 UUID（S5 拆库后 auth 库才是用户权威，业务库不做物理外键）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


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


class UserRelationship(UUIDPrimaryKeyMixin, Base):
    """用户间关系：``block``（双向语义，存单向行）与 ``mute``（单向）。

    - 每个方向一行，``UNIQUE(user_id, target_id, kind)`` 保证 block/mute 幂等；
    - block 的**语义是双向**：任何方向存在 block 行都视为两人互相屏蔽，判定见
      :meth:`UserRelationshipRepository.is_blocked_either_direction`；
    - mute 只影响 ``user_id`` 自己看到的内容/通知，不限制对方互动；
    - ``user_id`` / ``target_id`` 都是裸 UUID（auth 库才是用户权威，业务库不做物理外键）。
    """

    __tablename__: str = "user_relationships"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint(
            "user_id", "target_id", "kind", name="uq_user_relationships_kind"
        ),
        Index("ix_user_relationships_user_kind", "user_id", "kind"),
        Index("ix_user_relationships_target_kind", "target_id", "kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # 临时 block/mute：到期后按 degrade_to_status 降级（block → mute），空 = 永久。
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    degrade_to_status: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class FriendRelationship(UUIDPrimaryKeyMixin, Base):
    """好友关系/请求：一行一个方向（发起人 → 目标）。

    - ``status``：0 待接受、100 好友、200 亲密好友；block/mute 由
      :class:`UserRelationship` 的独立行走同一套状态阶梯，列表读时合并；
    - 待接受请求的 ``expires_at`` 到期后不再出现在请求列表；接受时清空；
    - 接受后保留发起方向，两端列表都由服务层以「对方」身份呈现。
    """

    __tablename__: str = "friend_relationships"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint(
            "account_id", "related_id", name="uq_friend_relationships_pair"
        ),
        Index(
            "ix_friend_relationships_account_status", "account_id", "status"
        ),
        Index(
            "ix_friend_relationships_related_status", "related_id", "status"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    related_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0
    )
    alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class ContentSubscription(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """统一内容订阅（软删墓碑）：板块 / 标签 / 专栏。

    - ``target_type`` 取 ``board|tag|column``；``target_id`` 统一存字符串——board/column
      存 UUID 字符串，tag 存**规范化标签**（trim + 小写）；
    - ``UNIQUE(user_id, target_type, target_id)`` + 软删墓碑 → 取消订阅后再订阅是复活同一行；
    - ``notify_level`` 取 ``all|none``，``none`` 表示「不提醒」（订阅仍存在，供列表展示）；
    - 原 ``board_follows`` 的语义完全被 ``target_type='board'`` 覆盖（迁移 0006 搬迁）。
    """

    __tablename__: str = "content_subscriptions"
    __table_args__: tuple[UniqueConstraint, Index] = (
        UniqueConstraint(
            "user_id",
            "target_type",
            "target_id",
            name="uq_content_subscriptions_target",
        ),
        # fanout：按目标反查订阅者
        Index("ix_content_subscriptions_target", "target_type", "target_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    notify_level: Mapped[str] = mapped_column(
        String(10), nullable=False, default="all"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class PostWatch(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """单帖 watch（软删墓碑）：收藏/评论后自动跟进，后续互动通知 watcher。

    ``UNIQUE(user_id, content_id)`` + 软删墓碑保证幂等；``notify_level`` 语义同
    :class:`ContentSubscription`。内容硬删时随 FK 级联清理。
    """

    __tablename__: str = "post_watches"
    __table_args__: tuple[UniqueConstraint, Index] = (
        UniqueConstraint("user_id", "content_id", name="uq_post_watches_pair"),
        # 通知 fanout：按内容反查 watcher
        Index("ix_post_watches_content", "content_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False
    )
    notify_level: Mapped[str] = mapped_column(
        String(10), nullable=False, default="all"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
