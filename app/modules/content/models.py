from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (  # 注意是 db.base 不是 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)
from app.modules.content.column_models import (
    ColumnApplicationStatus,
    ColumnPostStatus,
    ColumnStatus,
)

if TYPE_CHECKING:
    # 跨模块字符串 relationship 目标类型（运行时由 registry 解析，仅类型注解用）
    from app.modules.feed.models import BoardFollow


class ColumnApplication(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "column_applications"

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

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id 逻辑引用
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("column_applications.id"), unique=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # —— 栏目展示字段（供前端社区「专栏」页富展示，见 docs 方案）——
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
    __tablename__: str = "column_posts"

    column_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("columns.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id 逻辑引用
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


# M6.9 搜索 P1：可检索文本 → tsvector 生成列的表达式（写入时由 PG 自动维护）。
# ``simple`` 分词不识别中文（连续中文整段视为一个 lexeme），中文子串检索由
# ``pg_trgm`` GIN 索引承担（见下方 ix_content_*_trgm），两者互补。
SEARCH_VECTOR_SQL = (
    "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(excerpt,'') || "
    "' ' || coalesce(content,'') || ' ' || coalesce(summary,'') || ' ' || "
    "coalesce(keywords,'') || ' ' || coalesce(tags,''))"
)


class ContentItem(UUIDPrimaryKeyMixin, Base):
    """统一内容表：五套旧内容表（forum_posts/articles/column_posts/blog 发布产物）收敛。

    用 ``content_type`` 判别（discussion/article/column_post/blog_post），``board_id``
    作唯一分类轴，``author_id``（user FK）与 ``publisher``/``department``（官方字符串）
    二选一表达作者身份。``column_id`` 指向连载容器（仅 column_post 用）。
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
        Index("ix_content_slug", "slug"),
        # M6.9 搜索 P1：tsvector 生成列 GIN（英文/数字词）+ pg_trgm GIN（中文子串
        # ILIKE '%x%'）。trgm opclass 属 pg_trgm 扩展，**显式限定 public**——否则
        # 索引 DDL 依赖连接的 search_path（测试库 schema-per-test 不含 public 时会
        # 解析不到 opclass 而建表失败）。扩展由迁移 / init_db / tests/conftest 保证
        # 装在 public。
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
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True, index=True
    )
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
    # M6.9 搜索 P1：物化 tsvector（生成列，插入/更新时 PG 自动维护，应用不写）
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
    # M6.10 计数对账：本行计数最后一次**因对账被修正**的时刻（NULL = 从未偏差）。
    # 不是「上次扫描时刻」——故连续两次对账第二次不再触碰任何行，收敛可证伪。
    counts_reconciled_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
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
    column: Mapped[Column | None] = relationship()
    qa_question: Mapped[QAQuestion | None] = relationship(foreign_keys=[qa_question_id])
    comments: Mapped[list[ContentComment]] = relationship(
        back_populates="content_item", cascade="all, delete-orphan"
    )
    like_records: Mapped[list[ContentLike]] = relationship(
        back_populates="content", cascade="all, delete-orphan"
    )


class ContentComment(UUIDPrimaryKeyMixin, Base):
    """统一内容评论（对齐原 forum_comments 的完整模式：floor_number/parent_id/like_count）。"""

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

    content_item: Mapped[ContentItem] = relationship(back_populates="comments")
    # remote_side 用字符串：主键 id 来自 UUIDPrimaryKeyMixin，类体内无 `id` 名字绑定
    # （写 [id] 会解析到内置函数 id，configure_mappers 直接报错）
    parent: Mapped[ContentComment | None] = relationship(
        remote_side="ContentComment.id", back_populates="replies"
    )
    replies: Mapped[list[ContentComment]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
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


class QAQuestion(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_questions"
    # 问答列表按 category + id 倒序，status 用于状态筛选；无索引则列表页全表扫
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_question_category_id", "category", "id"),
        Index("ix_qa_question_status_id", "status", "id"),
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    bounty_people: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bounty_per_person: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounty_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounty_distributed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )  # open|accepted|closed
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="help"
    )  # help|volunteer（前端 tab 分类）
    accepted_answer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("qa_answers.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class QAAnswer(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_answers"
    # 按 question_id 拉回答 / 判定是否已采纳；无索引时按提问拉回答全表扫
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_answer_question", "question_id"),
        Index("ix_qa_answer_question_accepted", "question_id", "is_accepted"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("qa_questions.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class QAQuestionImage(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_question_images"
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_question_image_question", "question_id"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("qa_questions.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


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
    followers: Mapped[list[BoardFollow]] = relationship(
        back_populates="board",
        foreign_keys="BoardFollow.board_id",
        cascade="all, delete-orphan",
    )


class BoardApplication(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "board_applications"

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
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
