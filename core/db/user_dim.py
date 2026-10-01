"""
离线报表宽表 ``user_dim``。
.. note:: **OFFLINE-ONLY — 永不得作为在线读源。** 在线读路径一律走 ``user:snap`` 缓存 /
   ``auth.snapshot`` 实时缝（一致性由 user:snap/API 保证），**严禁**任何在线端点
   把本表当数据源。唯一写者是 auth 源侧的 ETL（B0.2，单独任务）；运营/报表/admin 报表读
   （B0.3，单独任务）才读它。数据语义归属 auth＝单一数据源owner。
本表非关系主体，无 relationship；仅只读镜像，绝不进入任何写事务参与方。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, UTCDateTime, now_iso


class UserDim(Base):
    """
    离线报表宽表：user/profile 登录锚字段的单源只读反范式副本（仅报表读）。
    read-only REPLICA：不替换、不改写 users/profiles，也不构成任何写事务参与方。唯一写者是
    B0.2 的 auth 源 ETL 回填；本任务（B0.1）只交付表定义 + registry 注册 + 迁移 + 存在性测试。
    """

    __tablename__: str = "user_dim"

    # PK 即源 user_id（宽表每用户恒一行，报表按 id 关联/过滤）。非自增——id 由 auth 源 ETL 显式给定
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)  # ← users.username
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)  # ← users.email
    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)  # ← profiles.nickname
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ← profiles.role
    account_level: Mapped[str] = mapped_column(String(10), nullable=False, default="local")  # ← users.account_level
    # banned 语义同在线缝 snapshot(banned=bool(User.is_locked))；is_locked 也传源镜像供对账
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # ← users.is_locked
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )  # ← users.created_at
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )  # ← users.updated_at
    # 每次 ETL 回填落的时间戳（B0.2 写；报表据此判数据新鲜度）
    sync_ts: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
