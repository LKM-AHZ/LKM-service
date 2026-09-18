"""离线报表宽表 ``user_dim``（M3.B0.1「物理建模」腿，纯增量定义）。

.. note:: **OFFLINE-ONLY — 永不得作为在线读源。** 在线读路径一律走 ``user:snap`` 缓存 /
   ``app.modules.auth.snapshot`` 实时缝（一致性由 user:snap/API 保证），**严禁**任何在线端点
   把本表当数据源。唯一写者是 auth 源侧的 ETL（B0.2，单独任务）；运营/报表/admin 报表读
   （B0.3，单独任务）才读它。数据语义归属 auth＝单一数据源owner。

背景：B0 是「先逻辑后物理」的报表前置。A1-A7 只建了在线用的 ``user:snap`` 缓存 + auth 读缝；
B0 另起一条**离线**报表支路：把 user/profile 的登录锚字段反范式摊平成一张只读宽表（单源 =
auth），供运营/报表这类批式、可容忍滞后、绝不容忍 PII 横向散布的读者，把「join
users+profiles + 解析账号状态」从报表 SQL 里抽出来提前物化一次。

为什么放 ``app/db/``（非 ``app/modules/auth/``）：与 ``event_failure`` / ``event_processed`` /
``outbox`` 这些**非业务模块 models.py** 的共享基础设施/审计表同范式 —— 本表是 read-only
反范式副本（离线物化），非 auth 业务模块的可执行逻辑，定义于 db 层让
``model_registry.ensure_all_models`` 记录的是 **db→db 内部边**（镜像它 import
``app.db.event_failure`` 把非 modules 的表拉进 metadata 的落位），从而**零新增跨层
import-linter 边**、保住契约二「db 层不反向依赖业务模块」冷跑 4 kept 0 broken。若置于
auth 会让 ``app.db.model_registry → app.modules.auth.*`` 成为新违约边（真实信号），故按仓库
db/ 基座的这些已确立落位收敛。字段语义仍严格对齐 auth 源（User/Profile），见下映射注释。

约束（verbatim）：
- 这是**全新的表**；绝不动 ``users`` / ``profiles`` / 任何在线缝；无 drop/alter。
- 纯增量：``model_registry.ensure_all_models``（dev create_all）经本文件 import 即建出此表；
  Alembic 链保持单头线性（revision ``f1a2e3d4c5b6a7f8``，down = ``a3f5b6c7d8e9afae``）。
- Mapped typed（对齐仓库 SQLAlchemy 2.0 风格）；时间列统一 ``UTCDateTime``。
- nickname/role 来自 profiles（nullable，join 左缺失时为空），其余来自 users。
- ``is_banned`` 与在线缝 ``app.modules.auth.snapshot._to_snap`` 语义一致：
  ``banned = bool(User.is_locked)``；名在此与 users ``is_locked`` 一样传源镜像，供报表分别对账、
  后续 B0.3 读者按此语义对齐不再歧义。
- ``sync_ts`` 为每次 ETL（B0.2）回填的写入时间戳，默认 now（报表可判新鲜度）。

本表非关系主体，无 relationship；仅只读镜像，绝不进入任何写事务参与方。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, now_iso


class UserDim(Base):
    """离线报表宽表：user/profile 登录锚字段的单源只读反范式副本（仅报表读，B0）。

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
