"""专栏（columns）域 ORM 模型：专栏、申请与专栏连载。

步骤 9 切片 3 自 ``content.models`` 迁出，**表名、列、索引一字不变**，无需迁移；
``content_items.column_id`` 仍是指向本模块 ``columns.id`` 的字符串外键，content 侧不
import 本模块的类（跨域的 ``column`` relationship 已随之删除）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.columns.column_models import (
    ColumnApplicationStatus,
    ColumnPostStatus,
    ColumnStatus,
)
from core.db.base import (  # 注意是 db.base 不是 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class ColumnApplication(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "column_applications"
    __table_args__ = (
        # 同一用户同时只能有一条待审专栏申请；被拒/已通过的行不占用该槽位
        # （冷却期由 service 层按 reviewed_at 判）。
        Index(
            "uq_column_applications_pending_user",
            "user_id",
            unique=True,
            postgresql_where=column("status") == "pending",
        ),
        Index("ix_column_applications_user_status", "user_id", "status"),
    )

    # S5 拆库后 user FK 断为逻辑 user_id（auth 独立库权威，此处不再物理外键）
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[ColumnApplicationStatus] = mapped_column(
        String(20), nullable=False, default=ColumnApplicationStatus.PENDING
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    column: Mapped[Column | None] = relationship(
        back_populates="application", uselist=False
    )


class Column(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "columns"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id 逻辑引用
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("column_applications.id"), unique=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    author_title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    author_bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subscribe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    badges: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    board_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("boards.id"), nullable=True
    )
    status: Mapped[ColumnStatus] = mapped_column(
        String(20), nullable=False, default=ColumnStatus.ACTIVE
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    application: Mapped[ColumnApplication | None] = relationship(
        back_populates="column"
    )
    posts: Mapped[list[ColumnPost]] = relationship(
        back_populates="column", cascade="all, delete-orphan"
    )


class ColumnPost(UUIDPrimaryKeyMixin, Base):
    """专栏原生连载（**尚未收敛进 content_items**）。

    ⚠️ 收敛现状：``ContentType.COLUMN_POST`` 与 ``content_items.column_id`` 已存在，但
    ``columns`` 域的「原生发帖」仍写本表（``columns/router.py`` POST /{column_id}/posts →
    ``service.create_column_post``，见 service.py 内注释「本表不经统一 content_items」），
    而 ``content/service.create_item`` 又能写 ``content_items`` 里 content_type=column_post
    的行。**同一篇专栏连载可能两表各存一份、id 不同**，读侧（feed 读 ColumnPost vs 时间线
    读 ContentItem）会看到不一致。以本表为专栏连载的权威存储；content_items 侧那份是投影，
    收敛（二选一 + 回填/对账）尚未完成，勿假定两者等价。
    """

    __tablename__: str = "column_posts"

    column_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("columns.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id 逻辑引用
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ColumnPostStatus] = mapped_column(
        String(20), nullable=False, default=ColumnPostStatus.PUBLISHED
    )
    cover_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    column: Mapped[Column] = relationship(back_populates="posts")
