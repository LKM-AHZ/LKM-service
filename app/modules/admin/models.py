from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (  # 注意 db.base 而非 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class Report(UUIDPrimaryKeyMixin, Base):
    """后台举报记录：用户对帖子/评论/文件等目标发起的举报，供后台审核。"""

    __tablename__: str = "reports"
    __table_args__: tuple[Any, ...] = (
        # 后台「按 status 分页列的待办举报」高频查询：条件 status + 排序 id desc
        Index("ix_reports_status_id", "status", "id"),
        Index("ix_reports_target", "type", "target_id"),
    )

    type: Mapped[str] = mapped_column(String(20), nullable=False)  # post/comment/file
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )  # S5: auth user_id
    reporter_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    handled_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class RolePermission(UUIDPrimaryKeyMixin, Base):
    """RBAC：复合角色→权限点 映射。角色即 ``{account_level}:{profile.role}``。"""

    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_name", "permission"),)

    role_name: Mapped[str] = mapped_column(String(40), nullable=False)
    permission: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class ModerationRule(UUIDPrimaryKeyMixin, Base):
    """自动审校规则（关键词/域名黑名单，正则可选）：读时降权 / 隐藏。

    * ``action="derank"``：命中后按 ``weight`` 压低 sort_score，不剔除。
    * ``action="hide"``：命中后直接在时间线合流前剔除。
    不改内容状态、无 pending/hidden 拦截状态机——纯读时评估。
    """

    __tablename__: str = "moderation_rules"

    # 关键词 / 域名 / 正则（is_regex=True）
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # derank | hide
    action: Mapped[str] = mapped_column(String(10), nullable=False, default="derank")
    # 降权力度 0..1（derank 用；hide 忽略直接过滤）
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # 命中范围：content=标题+正文；预留（后续可加 author/board 级）
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="content")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class DlqMessage(UUIDPrimaryKeyMixin, Base):
    """死信消息落库：worker_dlq 消费 lkm.dlq 队列持久化，供人工重投/审计。

    时间列遵循本文件既有约定：用 UTCDateTime 类型 + ``now_iso()`` 默认值
    （参考 LibraryFile.created_at）。勿用裸 ``DateTime`` / ``datetime.now(UTC)``。
    """

    __tablename__: str = "dlq_messages"

    routing_key: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    exchange: Mapped[str] = mapped_column(String(255), default="lkm.events")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    requeued_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
