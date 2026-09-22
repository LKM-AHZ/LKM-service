"""数据库声明基座：Base / Mixin / UTCDateTime / 时间辅助函数。

跨模块共享的 ORM 基础设施集中于此（计划 §2 db/base.py）。各模块 ``models.py``
从本模块 import ``Base``/``UUIDPrimaryKeyMixin``/``SoftDeleteMixin``/``UTCDateTime``/
``now_iso``/``expires_at``。本模块不依赖任何业务模块，保证 ``core/``、``db/`` 层不反向
依赖业务（import-linter 契约）。

注意：必须确保全部模块 ``models.py`` 都被导入后 SQLAlchemy 的 mapper registry
才会在 ``configure()`` 时解析到所有 relationship 字符串引用（见 db/registry 侧的
模型预注册）。
"""

from __future__ import annotations

import datetime
import uuid as _uuid
from typing import Any

from sqlalchemy import DateTime, TypeDecorator, Uuid, text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine


class Base(DeclarativeBase):
    pass


def now_iso() -> datetime.datetime:
    """当前 UTC 时间（timezone-aware），用于默认值与比较。"""
    return datetime.datetime.now(datetime.UTC)


def expires_at(days: float = 0, minutes: float = 0) -> datetime.datetime:
    """从现在起 days/minutes 后的 UTC 时间（timezone-aware）。"""
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        days=days, minutes=minutes
    )


class UTCDateTime(TypeDecorator[datetime.datetime]):
    """带时区的 UTC 时间列类型。底层使用 DateTime(timezone=True)"""

    impl: TypeEngine[Any] | type[TypeEngine[Any]] = DateTime(timezone=True)
    cache_ok: bool | None = True

    def process_bind_param(
        self, value: datetime.datetime | None, dialect: Dialect
    ) -> datetime.datetime | None:
        """naive 值一律按 UTC 解释后再落库（与读取侧口径一致）。

        没有这一层时，naive datetime 会被驱动的会话时区解释：实测本机会话时区是
        Asia/Shanghai 时，绑定 ``2026-01-01 12:00`` 存进去变成 ``04:00:00+00``——
        静默偏移 8 小时，且读写不对称（写按本地、读按 UTC）。
        """
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=datetime.UTC)
        return value

    def process_result_value(
        self, value: datetime.datetime | None, dialect: Dialect
    ) -> datetime.datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=datetime.UTC)
        return value


class UUIDPrimaryKeyMixin:
    """UUID 主键混入（时间有序）。

    主键为 PG 原生 ``uuid`` 类型，默认值由 ``uuid_generate_v7()`` 生成（RFC 9562
    uuid7：48 位毫秒时间戳 + 12 位亚毫秒，**跨进程与同毫秒内均单调递增**）——故既有
    ``order_by(id)`` 的「按时间先后」语义保持不变。

    两点必须注意：

    1. 函数由 ``init_db`` 建在 **public** schema（``deploy/initdb/`` 同样兜底），故
       ``server_default`` 必须**显式限定 schema**：测试的 schema-per-test 会把
       ``search_path`` 覆盖为测试 schema（不含 public），不限定即解析不到函数。
    2. 函数不存在时 ``CREATE TABLE`` 会立即报错（PG 建表即解析 DEFAULT 表达式），
       所以建表前必须先建函数——见 ``init_db`` 的扩展/函数装配段。
    """

    id: Mapped[_uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("public.uuid_generate_v7()")
    )


class SoftDeleteMixin:
    """软删除混入：``deleted_at`` 非空即视为已删除。

    仅对确有恢复诉求的表混入（内容/评论域 + 既有 starhope/feed）。查询侧过滤由
    Repository 基类按「模型是否真有该列」动态施加，见 ``app/db/repository.py``。
    列定义与既有手写列逐字一致（``UTCDateTime`` + nullable，无 default/index），
    故替换既有手写列为 mixin 时 schema 零变更。
    """

    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
