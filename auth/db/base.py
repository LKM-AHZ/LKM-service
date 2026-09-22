"""AUTH 独立库 ORM 基座：``AuthBase``/``auth_metadata``（M3.B 真拆库的元数据根）。

背景（M3.B S1）：author 物理拆独立库后，auth 自持表（users/profiles/refresh_tokens
等 17+ 张，见 auth/models.py）将迁到 auth 专属第二个 PostgreSQL。其 ORM 元数据
必须与 monolith 的全局 ``Base.metadata`` 分离——否则两库 schema 会互相污染（monolith
``Base.metadata.create_all``/alembic autogenerate 会把它带到主库）。

本模块只声明 :class:`AuthBase`（独立的 ``registry``/``metadata`` 根），**不含**任何映射。
S5 **已执行**：``auth/models.py`` 的 18 张表全部继承 ``AuthBase``，monolith 的
``Base.metadata`` 不再含 auth 表（``tests/test_init_db.py`` 断言了这一不变量）；auth 元数据
由 auth 进程独立 ``create_all`` / 第二 alembic 链（``alembic_auth``）持有。
**不要**把 auth 模型改挂回 monolith 的 ``Base``——那会让两库 schema 重新互相污染。

注意：本模块只依赖 SQLAlchemy，不依赖 app 包或 auth 包内任何其他模块，以免形成坏边。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class AuthBase(DeclarativeBase):
    """AUTH 独立库 ORM 根（auth.models 的 18 张表都挂在它上）。"""


# 聚合句柄：AUTH 库所有自持表的 metadata（供 create_all / alembic autogenerate 引用）。
# 使用前须确保已导入 auth.models（否则注册表为空）；auth.db.init.register_models / 各迁移
# 与单测都以此为前置。
auth_metadata = AuthBase.metadata
