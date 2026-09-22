"""通用 Repository 基类：把服务层反复出现的 CRUD 收敛成一处。

设计口径（见计划批 3）：

- **模型由子类类属性绑定**（``model``），主键用**运行期属性名** ``pk_attr``（默认
  ``"id"``）解析，不绑类型——UUID 与 Integer 主键天然兼容。
- **软删除动态判定**：模型上真有 ``deleted_at`` 列才施加过滤，无列零副作用；逃生口
  统一是 ``include_deleted=True``。
- **事务归属：只 flush，永不 commit/begin/begin_nested**。唯一提交主体仍是会话依赖
  （``app/db/session.py:get_session``、``auth/db/session.py:get_auth_session``）与
  最外层 task。基类里出现第二个提交主体会破坏「service 只 flush」契约。
- **只 import sqlalchemy / app.core / app.db**，守住 import-linter 契约②
  （``app.db`` 不得反向依赖 ``app.modules``）。业务域查询放
  ``app/modules/<domain>/repository.py`` 的子类里。

与 :mod:`app.db.repo` 的分工：那三个模块级函数（``get_or_raise`` / ``consume_once`` /
``isolated_update``）**原样保留**，各有独立语义（router 直接用、事务原语、savepoint）。
``consume_once`` / ``isolated_update`` 本基类不提供同名方法，避免出现两套签名；``get_or_raise``
两边都有但签名不同、语义互补——模块级按**任意条件**查（条件须最多命中一行，见其 docstring），
本基类 :meth:`AsyncRepository.get_or_raise` 按**主键**查并带软删过滤，调用时勿混淆两者。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import ColumnElement, Result, Select, delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.err import BizError, ErrCode

logger = logging.getLogger("lkm.db.repository")

ValuesDict = dict[str, Any]

#: 服务层的会话类型别名。service 层禁止 ``import sqlalchemy``（批 3 验收口径），
#: 故签名里的 ``db: AsyncSession`` 统一改写为从本模块取的 ``DbSession``——同一个类，
#: 只是把 sqlalchemy 的 import 收在 db 层内。
DbSession = AsyncSession


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class AsyncRepository[ModelT]:
    """模型化的异步仓储基类。

    子类只需声明 ``model``（必要时覆盖 ``pk_attr``）：:

        class ContentItemRepository(AsyncRepository[ContentItem]):
            model = ContentItem

            async def list_published(self, ...) -> list[ContentItem]:
                ...  # 领域查询放这里，SQLAlchemy 不过 service 层

    用法：service 保持 ``db: AsyncSession`` 第一参数不变，函数内
    ``repo = ContentItemRepository(db)`` 构造（成本≈0），不引入 FastAPI DI。
    """

    #: 被绑定的 ORM 模型（子类必须声明）。
    model: type[ModelT]
    #: 主键属性名。用属性名而非类型/列对象，兼容 UUID 与自增整数两种主键。
    pk_attr: str = "id"

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ─────────────────────── 元信息 ───────────────────────

    @property
    def pk_column(self) -> InstrumentedAttribute[Any]:
        """主键列对象（``getattr`` 运行期解析，子类换主键名无需改基类）。"""
        return getattr(self.model, self.pk_attr)

    @property
    def soft_delete_column(self) -> InstrumentedAttribute[Any] | None:
        """模型的 ``deleted_at`` 列，没有则 ``None``（软删过滤据此动态开合）。"""
        return getattr(self.model, "deleted_at", None)

    def _active_conditions(self, include_deleted: bool) -> list[ColumnElement[bool]]:
        """「未软删」条件；模型无 ``deleted_at`` 列或显式要含已删时返回空列表。"""
        column = self.soft_delete_column
        if column is None or include_deleted:
            return []
        return [column.is_(None)]

    # ─────────────────────── 查询 ───────────────────────

    async def get(
        self,
        pk: Any,
        *,
        include_deleted: bool = False,
        options: tuple[Any, ...] = (),
    ) -> ModelT | None:
        """按主键取一行，未命中返回 ``None``。"""
        stmt = select(self.model).where(
            self.pk_column == pk, *self._active_conditions(include_deleted)
        )
        if options:
            stmt = stmt.options(*options)
        return (await self.db.execute(stmt)).scalars().first()

    async def get_or_raise(
        self,
        pk: Any,
        errcode: ErrCode,
        *,
        detail: str | None = None,
        include_deleted: bool = False,
        options: tuple[Any, ...] = (),
    ) -> ModelT:
        """按主键取一行，未命中抛 ``BizError(errcode)``。"""
        obj = await self.get(pk, include_deleted=include_deleted, options=options)
        if obj is None:
            raise BizError(errcode, detail)
        return obj

    async def get_one(
        self,
        *conditions: Any,
        include_deleted: bool = False,
        options: tuple[Any, ...] = (),
        order_by: Any = None,
    ) -> ModelT | None:
        """按条件取首行，未命中返回 ``None``。"""
        stmt = self._select(
            *conditions, include_deleted=include_deleted, options=options
        )
        if order_by is not None:
            stmt = stmt.order_by(*_as_tuple(order_by))
        return (await self.db.execute(stmt)).scalars().first()

    async def get_one_or_raise(
        self,
        errcode: ErrCode,
        *conditions: Any,
        detail: str | None = None,
        include_deleted: bool = False,
        options: tuple[Any, ...] = (),
        order_by: Any = None,
    ) -> ModelT:
        """按条件取首行，未命中抛 ``BizError(errcode)``。"""
        obj = await self.get_one(
            *conditions,
            include_deleted=include_deleted,
            options=options,
            order_by=order_by,
        )
        if obj is None:
            raise BizError(errcode, detail)
        return obj

    async def get_many(
        self,
        *conditions: Any,
        order_by: Any = None,
        offset: int | None = None,
        limit: int | None = None,
        options: tuple[Any, ...] = (),
        include_deleted: bool = False,
    ) -> list[ModelT]:
        """按条件取多行。``order_by`` 接受单个表达式或表达式序列。"""
        stmt = self._select(
            *conditions, include_deleted=include_deleted, options=options
        )
        if order_by is not None:
            stmt = stmt.order_by(*_as_tuple(order_by))
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def count(self, *conditions: Any, include_deleted: bool = False) -> int:
        """满足条件的行数（默认不含已软删）。"""
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(*conditions, *self._active_conditions(include_deleted))
        )
        return await self.db.scalar(stmt) or 0

    async def exists(self, *conditions: Any, include_deleted: bool = False) -> bool:
        """条件是否至少命中一行（比 count 少一次聚合）。"""
        stmt = (
            select(self.pk_column)
            .where(*conditions, *self._active_conditions(include_deleted))
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    def _select(
        self,
        *conditions: Any,
        include_deleted: bool,
        options: tuple[Any, ...],
    ) -> Select[Any]:
        stmt = select(self.model).where(
            *conditions, *self._active_conditions(include_deleted)
        )
        if options:
            stmt = stmt.options(*options)
        return stmt

    # ─────────────────────── 写入 ───────────────────────

    async def create(self, **values: Any) -> ModelT:
        """按字段建行并 flush，返回新实例（刷新后主键/默认值已就绪）。"""
        obj = self.model(**values)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def add(self, obj: ModelT) -> ModelT:
        """落盘一个已构造好的实例（字段较多时比 ``create(**`` 可读）并 flush。"""
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, obj: ModelT, **values: Any) -> ModelT:
        """就地改属性并 flush（走 ORM dirty 追踪，不额外发 UPDATE 语句构造）。"""
        for key, value in values.items():
            setattr(obj, key, value)
        await self.db.flush()
        return obj

    async def update_where(self, values: ValuesDict, *conditions: Any) -> int:
        """批量 UPDATE，返回受影响行数（**不**施加软删过滤，谓词由调用方给全）。"""
        # 空谓词 = 全表改写/全表删除且静默返回行数，几乎必然是漏传条件；宁可直接炸
        if not conditions:
            raise ValueError("update_where 至少需要一个条件，禁止无谓词全表更新")
        if not values:
            raise ValueError("update_where 的 values 不能为空，否则生成非法 SQL")
        result: Result[Any] = await self.db.execute(
            sa_update(self.model).where(*conditions).values(**values)
        )
        await self.db.flush()
        return result.rowcount or 0

    async def delete(self, obj: ModelT) -> None:
        """硬删一个实例并 flush（模型有 ``deleted_at`` 时优先用 :meth:`soft_delete`）。"""
        await self.db.delete(obj)
        await self.db.flush()

    async def hard_delete_where(self, *conditions: Any) -> int:
        """按条件批量硬删，返回受影响行数。"""
        if not conditions:
            raise ValueError("hard_delete_where 至少需要一个条件，禁止无谓词全表删除")
        result: Result[Any] = await self.db.execute(
            delete(self.model).where(*conditions)
        )
        await self.db.flush()
        return result.rowcount or 0

    # ─────────────────────── 软删除 ───────────────────────

    async def soft_delete(
        self, obj: ModelT, *, at: datetime.datetime | None = None
    ) -> None:
        """打软删时间戳并 flush；模型没有 ``deleted_at`` 列时退化为硬删。

        ``soft_delete_column is None`` 的分支会**不可恢复地**物理删除（且 ``restore``
        退化为空操作），调用方若按「可撤销删除」使用就会丢数据——故至少留下 error 级日志，
        让「调错了模型」这件事在日志里可见。
        """
        if self.soft_delete_column is None:
            logger.error(
                "soft_delete 被用于无 deleted_at 列的模型 %s：退化为硬删（不可恢复）",
                type(obj).__name__,
            )
            await self.delete(obj)
            return
        obj.deleted_at = at or _utcnow()  # type: ignore[attr-defined]
        await self.db.flush()

    async def restore(self, obj: ModelT) -> None:
        """撤销软删（``deleted_at`` 置空）并 flush；模型无该列时是空操作。"""
        if self.soft_delete_column is None:
            return
        obj.deleted_at = None  # type: ignore[attr-defined]
        await self.db.flush()

    async def soft_delete_where(
        self, *conditions: Any, at: datetime.datetime | None = None
    ) -> int:
        """按条件批量软删（只命中原先未删的行），返回受影响行数。

        模型无 ``deleted_at`` 列时退化为 :meth:`hard_delete_where`。
        """
        column = self.soft_delete_column
        if column is None:
            return await self.hard_delete_where(*conditions)
        result: Result[Any] = await self.db.execute(
            sa_update(self.model)
            .where(*conditions, column.is_(None))
            .values(deleted_at=at or _utcnow())
        )
        await self.db.flush()
        return result.rowcount or 0

    # ─────────────────────── upsert ───────────────────────

    async def pg_upsert(
        self,
        values: ValuesDict | list[ValuesDict],
        *,
        index_elements: list[str] | None = None,
        constraint: str | None = None,
        update_columns: list[str] | None = None,
        update_values: ValuesDict | None = None,
        do_nothing: bool = False,
    ) -> None:
        """PostgreSQL ``INSERT ... ON CONFLICT``。

        - ``do_nothing=True``：命中冲突即跳过（``update_*`` 被忽略）。
        - ``update_columns``：冲突时把这些列更新为**本次提议值**（``excluded.<col>``）。
        - ``update_values``：冲突时写入的显式常量（如 ``{"updated_at": now}``）。
        - 二者可并用；解析目标二选一：``index_elements`` 或 ``constraint``。
        """
        stmt = pg_insert(self.model).values(values)
        if do_nothing:
            await self.db.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=index_elements, constraint=constraint
                )
            )
        else:
            # 两种组合会拼出非法 SQL 且只在执行期才暴露，故在构造语句前显式校验
            if not update_columns and not update_values:
                raise ValueError(
                    "pg_upsert 非 do_nothing 时必须给出 update_columns 或 update_values"
                    "（否则 ON CONFLICT DO UPDATE SET 无赋值项）"
                )
            if index_elements is None and constraint is None:
                raise ValueError(
                    "pg_upsert 的 ON CONFLICT DO UPDATE 必须给出冲突目标"
                    "（index_elements 或 constraint）"
                )
            set_: ValuesDict = dict(update_values or {})
            for column in update_columns or []:
                set_[column] = getattr(stmt.excluded, column)
            await self.db.execute(
                stmt.on_conflict_do_update(
                    index_elements=index_elements, constraint=constraint, set_=set_
                )
            )
        await self.db.flush()

    # ─────────────────────── 事务边界 ───────────────────────

    async def flush(self) -> None:
        """显式 flush（会话由最外层依赖/task commit，本层永不提交）。"""
        await self.db.flush()


def _as_tuple(order_by: Any) -> tuple[Any, ...]:
    return order_by if isinstance(order_by, (tuple, list)) else (order_by,)
