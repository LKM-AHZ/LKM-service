"""
数据访问原语：查询、一次性消费、savepoint 隔离更新。
服务层里反复出现的三件事都归约到这里，统一用 SQLAlchemy 条件表达式：
  get_or_raise   —— 查一行，没查到就抛领域错误
  consume_once   —— 条件满足才恰好更新一行（一次性 token/事务消费）
  isolated_update—— 在 savepoint 里更新，单条语句失败不污染调用方事务
"""

import logging
from contextlib import suppress
from typing import Any

from sqlalchemy import Result, Select, Update, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, ErrCode

logger = logging.getLogger("lkm.db.repo")


async def get_or_raise[M](
    db: AsyncSession,
    model: type[M],
    errcode: ErrCode,
    *conditions: Any,
    detail: str | None = None,
    options: tuple[Any, ...] = (),
) -> M:
    """按条件查一行，未命中则抛出 ``BizError(errcode)``。"""
    stmt: Select[Any] = select(model).where(*conditions)
    if options:
        stmt = stmt.options(*options)
    result: Result[Any] = await db.execute(stmt)
    obj: M | None = result.scalars().first()
    if obj is None:
        raise BizError(errcode, detail)
    return obj


async def consume_once[M](
    db: AsyncSession,
    model: type[M],
    values: dict[str, object],
    *conditions: Any,
) -> bool:
    """
    用于一次性 token / 恢复事务 / 挑战码的原子消费，防止并发重放。

    契约是「条件恰好确定一行」：匹配多行说明条件写漏，那些行**已经全部被改**，
    调用方却会收到 ``False``（看着像「已被消费」）——故此处显式记 error 暴露，
    不再把 rowcount>1 与「没命中」混为一谈。
    """
    # 空条件 = 无谓词全表改写（同 repository.update_where 的约定：几乎必然是漏传条件，宁可直接炸）
    if not conditions:
        raise ValueError("consume_once 至少需要一个条件，禁止无谓词全表更新")
    result = await db.execute(sa_update(model).where(*conditions).values(**values))
    await db.flush()
    count = getattr(result, "rowcount", 0) or 0
    if count > 1:
        logger.error(
            "consume_once 命中 %d 行（契约要求恰好一行，条件写漏？已全部改值）model=%s",
            count,
            getattr(model, "__name__", model),
        )
    return count == 1


async def isolated_update(db: AsyncSession, stmt: Update) -> None:
    """
    失败计数器等「本语句失败不拖垮调用方事务」的修改用它。

    **注意 savepoint 的边界**：``begin_nested`` 只发 ``SAVEPOINT``、``sp.commit()``
    只是 ``RELEASE SAVEPOINT``，改动仍是**外层事务的一部分**——调用方事务若随后
    ``rollback()``（如 ``app/db/session.py::get_session`` 在请求抛错时的行为），
    本次更新同样会被撤销。要「即使调用方回滚也保留」必须用独立会话/连接自行
    commit（autonomous transaction），此处不提供该语义。

    - ``IntegrityError``：可预期冲突（唯一约束撞车等），静默吞去重语义保留。
    - ``OperationalError``：可能是死锁/序列化失败/锁超时等真异常——虽仍回滚
      savepoint 不向调用方抛（保持原有不抛语义），但必须记 warning 暴露出来，
      否则计数/核销漏更会静默无日志。
    """
    sp = await db.begin_nested()
    try:
        await db.execute(stmt)
        await db.flush()
        await sp.commit()
    except IntegrityError:
        await _safe_rollback(sp)
    except OperationalError:
        await _safe_rollback(sp)
        logger.warning(
            "isolated_update OperationalError swallowed (rollback only this update), "
            "this may lose a counter/consume: %s",
            stmt,
        )
    except Exception:
        # 其它 DBAPI 失败（StatementError/ProgrammingError/InterfaceError/DataError…）：
        # 原先不回滚就往外抛，savepoint 悬着、会话进入 failed 状态，调用方后续语句全废
        await _safe_rollback(sp)
        raise


async def _safe_rollback(sp: Any) -> None:
    """回滚 savepoint 且不让回滚自身的失败掩盖原始异常。

    OperationalError 是连接级时（连接已失效）``sp.rollback()`` 自身也会抛——
    那会顶掉「本语句失败」这个真正的原因，也破坏 isolated_update「不向调用方抛」的语义。
    """
    with suppress(Exception):
        await sp.rollback()
