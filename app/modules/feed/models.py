from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (  # 注意 db.base 而非 db.models
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)

if TYPE_CHECKING:
    from app.modules.content.models import Board


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


class FeedItemMaterialized(UUIDPrimaryKeyMixin, Base):
    """时间线物化读模型（M6.11）：一条 = 某用户的 feed 里的一条内容。

    写扩散（fanout）由 cron 按源水位扫描新内容后为本条目的**关注者**写入；读路径
    只查本表（``(user_id, created_at, id)`` 游标），不再实时多源合流——实时合流降为
    「物化未命中」的兜底。

    ``(user_id, item_type, source_id)`` 唯一：重复 fanout 幂等（``ON CONFLICT DO
    NOTHING``），也是水位重放的安全网。``sort_score`` 存的是源侧热度分（写入时快照），
    读时再叠加时效/关注/审校权重（与实时合流同一套计算）。
    """

    __tablename__: str = "feed_items"
    __table_args__: tuple[UniqueConstraint, Index, Index] = (
        UniqueConstraint("user_id", "item_type", "source_id", name="uq_feed_item"),
        # 读路径游标：user_id + 时间倒序 + id 倒序（PG 可反向扫该索引）
        Index("ix_feed_items_user_cursor", "user_id", "created_at", "id"),
        Index("ix_feed_items_source", "item_type", "source_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # feed 所有者
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    board_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sort_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 展示字段快照：读路径零回查（内容编辑后 feed 里短暂显示旧标题，随新条目自然滚出）
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    content_preview: Mapped[str] = mapped_column(
        String(300), nullable=False, default=""
    )
    url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # 可见时间（与各源 FeedItem.created_at 同义，是跨源排序锚点）
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)


class FeedFanoutState(Base):
    """fanout 水位（M6.11）：每个内容源最后处理到的 ``(created_at, id)``。

    水位只推进到「已完整 fanout 的最大条目」，故中途失败重放不会漏（重复由
    ``feed_items`` 的唯一约束吸收）。``created_at`` 用 UTC。
    """

    __tablename__: str = "feed_fanout_state"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    # 水位末条的**内容主键**（源内 id，非时间）；首次无水位为 NULL
    last_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )
