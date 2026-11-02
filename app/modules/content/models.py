from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    column,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import (  # 注意是 db.base 不是 db.models
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class ContentType(StrEnum):
    """统一内容模型的内容体裁判别列。

    五套旧内容表（forum_posts / articles / column_posts / blog 发布产物）收敛为
    一张 content_items 表，用本枚举区分展示语义：
    - discussion：普通讨论帖（原 forum_posts，无审稿，发即 published）
    - article：官方发布文章（原 articles，含官方字段 publisher/department，含 news 分类）
    - column_post：专栏连载（原 column_posts，挂 column_id，追更）
    - blog_post：博客发布产物（原 blog_series 发布后落成的展示内容）
    """

    DISCUSSION = "discussion"
    ARTICLE = "article"
    COLUMN_POST = "column_post"
    BLOG_POST = "blog_post"
    QA = "qa"


class ContentStatus(StrEnum):
    """content_items.status 状态机。

    各体裁对齐旧语义：
    - discussion 恒 PUBLISHED（发即公开）
    - article / column_post / blog_post 支持 draft / pending / published / rejected
    """

    DRAFT = "draft"
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"


SEARCH_VECTOR_SQL = (
    "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(excerpt,'') || "
    "' ' || coalesce(content,'') || ' ' || coalesce(summary,'') || ' ' || "
    "coalesce(keywords,'') || ' ' || coalesce(tags,''))"
)


class ContentItem(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """统一内容表：五套旧内容表（forum_posts/articles/column_posts/blog 发布产物）收敛。

    用 ``content_type`` 判别（discussion/article/column_post/blog_post），``board_id``
    作唯一分类轴，``author_id``（user FK）与 ``publisher``/``department``（官方字符串）
    二选一表达作者身份。``column_id`` 指向连载容器（仅 column_post 用）。

    软删（批 4）：``deleted_at`` 非空即已删，可见性由 Repository 基类统一过滤；
    ``slug`` 非唯一但唯一性检查含墓碑行（``include_deleted=True``），防删后同 slug 复活歧义。
    """

    __tablename__: str = "content_items"
    __table_args__: tuple[Any, ...] = (
        Index(
            "ix_content_board_type_status", "board_id", "content_type", "status", "id"
        ),
        Index("ix_content_board_pinned", "board_id", "is_pinned", "id"),
        Index("ix_content_status_pinned", "status", "is_pinned", "id"),
        # 时间线按体裁扫描：content_type+status 过滤，created_at,id 排序，命中索引免 filesort
        Index(
            "ix_content_type_status_created",
            "content_type",
            "status",
            "created_at",
            "id",
        ),
        Index("ix_content_published", "published_at"),
        Index("ix_content_slug", "slug", unique=True),
        Index("ix_content_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_content_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_content_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "public.gin_trgm_ops"},
        ),
    )

    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # 统一分类轴
    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("boards.id"), nullable=False
    )
    # 作者：user id（S5 拆库后逻辑 user_id，auth 独立库权威）与官方字符串二选一
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    publisher: Mapped[str | None] = mapped_column(String(100), nullable=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 专栏连载容器（仅 column_post）
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("columns.id"), nullable=True, index=True
    )
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # QA 提问关联（仅 content_type == 'qa'）：指向 qa_questions，论坛条目可跳转提问详情
    qa_question_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("qa_questions.id"), nullable=True, index=True
    )
    # 内容本体
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    excerpt: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cover: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(SEARCH_VECTOR_SQL, persisted=True),
        nullable=True,
    )
    # 状态：discussion 恒 published；其余支持 draft/pending/published/rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 互动计数（论坛完整模式）
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bookmark_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forward_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counts_reconciled_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    board: Mapped[Board] = relationship()
    like_records: Mapped[list[ContentLike]] = relationship(
        back_populates="content", cascade="all, delete-orphan"
    )


class ContentLike(Base):
    """统一内容点赞记录，复合主键保证同一用户对同一内容最多一条（点赞幂等）。"""

    __tablename__: str = "content_likes"

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_items.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    content: Mapped[ContentItem] = relationship(back_populates="like_records")


class Board(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "boards"

    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )  # S5: auth user_id
    # 子板块挂父板块（板块广场嵌套展示：父=大分类，子=细分板块）
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("boards.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active | inactive
    # 发言准入配置
    require_certified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    daily_post_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # 0=不限
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 乐观锁版本号（蓝图 §6.1）：板块编辑用；语义与 ContentItem.version 一致。
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    parent: Mapped[Board | None] = relationship(
        remote_side="Board.id", back_populates="children"
    )
    children: Mapped[list[Board]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class BoardApplication(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "board_applications"
    # slug 不再全局唯一：被拒的申请必须释放标识（否则同 slug 永远无法重新申请），
    # 但 pending/approved 仍要拦住重复占用。冷却期由 service 层按 reviewed_at 判。
    __table_args__ = (
        Index(
            "uq_board_applications_slug_active",
            "slug",
            unique=True,
            postgresql_where=column("status") != "rejected",
        ),
        Index("ix_board_applications_applicant_slug", "applicant_id", "slug"),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending|approved|rejected
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


class BoardBan(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "board_bans"
    __table_args__ = (Index("ix_board_bans_board_user", "board_id", "user_id"),)

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("boards.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
