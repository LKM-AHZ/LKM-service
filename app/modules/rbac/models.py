"""RBAC 的 ORM 模型。

``role_permissions`` 原先定义在 ``admin.models``，导致「任何经 rbac 判权的模块」
都间接依赖 admin 域（``allow_indirect_imports=false`` 下会直接违约）。权限映射表
属于 RBAC 本身，放在本域后间接依赖消失；表名与列定义保持不变，无需迁移。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, now_iso


class RolePermission(UUIDPrimaryKeyMixin, Base):
    """RBAC：复合角色→权限点映射；禁用行保留显式撤销状态。"""

    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_name", "permission"),)

    role_name: Mapped[str] = mapped_column(String(40), nullable=False)
    permission: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
