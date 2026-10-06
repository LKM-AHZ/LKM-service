from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


class FileStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


class FileClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


# 文件库模块实际用到/计划的库表及其列（供 /files/status 健康自检展示）。
FILES_TABLE_PLAN = {
    "library_files": [
        "id",
        "uploader_id",
        "original_name",
        "stored_name",
        "sha3_hash",
        "ref_count",
        "storage_path",
        "mime_type",
        "size",
        "category_id",
        "description",
        "tags",
        "status",
        "review_comment",
        "download_count",
        "view_count",
        "created_at",
        "document_code",
        "classification",
        "project_id",
        "version",
        "root_file_id",
        "extracted_text",
        "archive_state",
        "backed_up_at",
    ],
}


class LibraryFile(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "library_files"
    __table_args__ = (
        Index("ix_library_files_sha3_hash", "sha3_hash"),
        UniqueConstraint(
            "document_code", "version", name="uq_library_document_version"
        ),
        Index("ix_library_files_project", "project_id"),
        Index("ix_library_files_root_version", "root_file_id", "version"),
        # 文件名 / 正文模糊搜索（trgm GIN）。opclass 走 public 前缀：扩展由
        # core.db.shared_objects 保证装在 public（schema-per-test 的 search_path 不含 public）。
        Index(
            "ix_library_files_name_trgm",
            "original_name",
            postgresql_using="gin",
            postgresql_ops={"original_name": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_library_files_text_trgm",
            "extracted_text",
            postgresql_using="gin",
            postgresql_ops={"extracted_text": "public.gin_trgm_ops"},
        ),
    )

    uploader_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # auth user_id
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # 内容寻址哈希（SHA3-256，16 进制 64 字符）
    sha3_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 引用计数：同一内容仍处于待审/已通过的条目数，归零时清理物理对象。
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 物理文件落盘路径（内容寻址：``files_store_dir/<hash[:2]>/<hash>``）。同一内容条目共享同一
    # ``storage_path``（不唯一），去重共享物理文件的关键；``stored_name`` 保持唯一作展示/定位。
    storage_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    document_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    classification: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FileClassification.PUBLIC,
        server_default="public",
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    root_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("library_files.id"), nullable=True
    )
    extracted_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    archive_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    backed_up_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class UploadSession(Base):
    """预签名直传会话（S3 直传的元数据载体）。

    **为什么落 DB**：这段元数据原存在 Redis 键 ``upload:{uid}``（无 TTL）。关掉 Redis
    持久化后重启会让在途会话全部蒸发——``confirm_upload`` 报 ``UPLOAD_EXPIRED``，且孤儿
    清扫原本靠 SCAN 这些键，键没了则 ``up/<uid>`` 对象**永久泄漏**（无记录可回溯）。落表
    后这两件事都不再依赖 Redis 存活。

    会话在 ``confirm_upload`` / ``notify_upload`` 认领时**删行**（等价原 ``GETDEL`` 的原子
    消费，以 DELETE RETURNING 获取归属）；未被认领的行由 ``cleanup_expired_uploads`` 按
    ``created_at`` 判龄回收。
    """

    __tablename__: str = "upload_sessions"
    __table_args__: tuple[Any, ...] = (
        Index("ix_upload_sessions_created", "created_at"),
    )

    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    uploader_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # 直传落地的随机对象 key（``up/<uid>``）：清扫据此删对象
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    meta: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
