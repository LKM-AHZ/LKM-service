from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


class FileStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


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
    ],
}


class LibraryFile(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "library_files"

    uploader_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # S5: auth user_id
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # 内容寻址哈希（SHA3-256，16 进制 64 字符）
    sha3_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 引用计数：同一物理文件被多少条目引用，归零时清理磁盘文件。DB 持久化，替代内存 cache。
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 物理文件落盘路径（内容寻址：``files_store_dir/<hash[:2]>/<hash>``）。同一内容条目共享同一
    # ``storage_path``（不唯一），去重共享物理文件的关键；``stored_name`` 保持唯一作展示/定位。
    storage_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
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
    消费，以 DELETE 影响行数判定归属）；未被认领的行由 ``cleanup_expired_uploads`` 按
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
    # 登记所需的完整元数据（JSON），与原先 Redis 标记的值逐字相同
    meta: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
