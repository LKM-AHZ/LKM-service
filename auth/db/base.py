"""AUTH 独立库 ORM 基座：``AuthBase``/``auth_metadata``（M3.B 真拆库的元数据根）。

背景（M3.B S1）：author 物理拆独立库后，auth 自持表（users/profiles/refresh_tokens
等 17+ 张，见 auth/models.py）将迁到 auth 专属第二个 PostgreSQL。其 ORM 元数据
必须与 monolith 的全局 ``Base.metadata`` 分离——否则两库 schema 会互相污染（monolith
``Base.metadata.create_all``/alembic autogenerate 会把它带到主库）。

本模块只声明 :class:`AuthBase`（独立的 ``registry``/``metadata`` 根），**不含**任何映射。
auth 表挂在哪个 metadata 上由装配时序决定：
- S1–S4（蓝绿共存）：auth.models 仍挂在 monolith 的 ``Base`` 上，此 ``AuthBase`` 为空，
  保证 monolith ``create_all`` 照建 auth 表、860 回归不受影响。
- S5：把 auth.models 全部迁到本 ``AuthBase`` 后，monolith 不再含 auth 表；auth 元数据
  由 auth 进程独立 create_all / 第二 alembic 链（``alembic_auth``）持有。

注意：本模块只依赖 SQLAlchemy，不依赖 app 包或 auth 包内任何其他模块，以免形成坏边。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class AuthBase(DeclarativeBase):
    """AUTH 独立库 ORM 根（S1 预留；auth.models 待 S5 迁入）。"""


# 聚合句柄：AUTH 库所有自持表的 metadata（供 create_all / alembic autogenerate 引用）。
# 在 auth.models 迁入前它是空的；迁移命令/单测以「确保导入 auth.models(迁后)」为前提。
auth_metadata = AuthBase.metadata
