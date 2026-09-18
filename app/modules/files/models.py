from __future__ import annotations

import datetime
import uuid
from enum import StrEnum

from sqlalchemy import Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


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
