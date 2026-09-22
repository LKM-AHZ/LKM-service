from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class BlogSeriesStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


BLOG_TABLE_PLAN = {
    "blog_series": [
        "id",
        "owner_id",
        "title",
        "description",
        "cover_url",
        "repo_name",
        "status",
        "created_at",
        "updated_at",
    ],
    "blog_stars": [
        "user_id",
        "series_id",
        "created_at",
    ],
    "blog_comments": [
        "id",
        "user_id",
        "series_id",
        "content",
        "parent_id",
        "created_at",
        "updated_at",
        # BlogComment 带 SoftDeleteMixin：漏掉 deleted_at 会让这份表结构说明与实际模型不符
        "deleted_at",
    ],
    "blog_content": [
        "id",
        "series_id",
        "path",
        "content",
        "sha3",
        "version",
        "created_at",
        "updated_at",
    ],
    "blog_repo_quarantine": [
        "id",
        "repo_name",
        "src_dir",
        "quarantined_at",
        "created_at",
        "updated_at",
    ],
}


class BlogSeries(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "blog_series"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[BlogSeriesStatus] = mapped_column(
        String(20), nullable=False, default=BlogSeriesStatus.ACTIVE
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    comments: Mapped[list[BlogComment]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )
    stars: Mapped[list[BlogStar]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )
    contents: Mapped[list[BlogContent]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )


class BlogStar(Base):
    __tablename__: str = "blog_stars"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True
    )  # S5: auth user_id
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("blog_series.id"), primary_key=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    series: Mapped[BlogSeries] = relationship(back_populates="stars")


class BlogComment(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    """博客系列评论。软删（批 4）：``deleted_at`` 非空即已删，列表与计数统一过滤。"""

    __tablename__: str = "blog_comments"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    # series_id / parent_id 都建索引：前者是「按系列列评论/计数」的热路径，
    # 后者是回复树按父查子；原先两者全无索引，随评论量增长退化为顺序扫描
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("blog_series.id"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("blog_comments.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    series: Mapped[BlogSeries] = relationship(back_populates="comments")
    # remote_side 用字符串：主键 id 来自 UUIDPrimaryKeyMixin，类体内无 `id` 名字绑定
    # （写 [id] 会解析到内置函数 id，configure_mappers 直接报错）
    parent: Mapped[BlogComment | None] = relationship(
        remote_side="BlogComment.id", back_populates="replies"
    )
    replies: Mapped[list[BlogComment]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class BlogContent(UUIDPrimaryKeyMixin, Base):
    """系列仓库内单个文件的正文（DB 为主存储）。

    每 series 下每个 path 一行（UniqueConstraint(series_id, path)），保存当前内容。
    git 裸仓库降级为版本快照层，文本内容的事实源在此表。``sha3`` 记录内容指纹，
    供将来 git 快照经 ``git http-backend`` push 时做变更检测/懒回填。
    """

    __tablename__: str = "blog_content"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("series_id", "path", name="uq_blog_content_series_path"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("blog_series.id"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha3: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    series: Mapped[BlogSeries] = relationship()


class BlogRepoQuarantine(UUIDPrimaryKeyMixin, Base):
    """被周对账隔离的孤儿 git 仓库台账（隔离=仅入库不移动目录）。

    repo_name 对应 ``<repo_name>.git``；src_dir 记录隔离前绝对路径便于恢复。
    对账按 quarantined_at 年龄超阈值才物理删除目录。delete_series 正常删仓库时同步清行。
    """

    __tablename__: str = "blog_repo_quarantine"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("repo_name", name="uq_blog_repo_quarantine_repo_name"),
    )

    repo_name: Mapped[str] = mapped_column(String(120), nullable=False)
    src_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    quarantined_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )
