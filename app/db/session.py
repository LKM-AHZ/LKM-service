import logging
import threading
from collections.abc import AsyncIterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.err import (
    AuthErr,  # M3 peer: 并入共享 shared err
    BizError,
)

# —— 主库（monolith，realm="default"）的惰性单例。M3.B 物理拆目标：monolith 主进程
# 只触达 database_url（auth 独立库走单独的 auth/db/session.py，主进程不侧挂）。
_async_engine: AsyncEngine | None = None
_AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None
# 惰性单例的双检锁（见 get_async_engine 注释）：sync 依赖可能来自线程池，故用
# threading.Lock 而非 asyncio.Lock（后者会绑定事件循环）
_engine_lock = threading.Lock()
# PostgreSQL SQLSTATE：唯一约束冲突
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def create_realm_async_engine(
    url: str,
    *,
    pool_size: int | None = None,
    pool_max_overflow: int | None = None,
    pool_pre_ping: bool | None = None,
) -> AsyncEngine:
    """按池参数建立 PostgreSQL(asyncpg) async 引擎。

    供主库（:func:`get_async_engine`）与 auth 独立库（auth/db/session.py）共用的唯一
    建池逻辑，避免两处策略漂移。
    """
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": False, "connect_args": connect_args}
    if pool_size is not None:
        engine_kwargs["pool_size"] = pool_size
    if pool_max_overflow is not None:
        engine_kwargs["max_overflow"] = pool_max_overflow
    if pool_pre_ping is not None:
        engine_kwargs["pool_pre_ping"] = pool_pre_ping
    return create_async_engine(url, **engine_kwargs)


def _ensure_engine_locked() -> AsyncEngine:
    """**调用方必须已持有 `_engine_lock`** 时使用；返回惰性单例引擎。

    存在的唯一理由：`_get_async_session_local` 要在持锁状态下绑定引擎，而 `_engine_lock`
    是 `threading.Lock`（**非重入**）。它若在那里直接调 `get_async_engine()`，而 `_async_engine`
    恰为 None（冷启动，或 `dispose_engine()` 之后），`get_async_engine` 会再取同一把锁
    ——**同一个线程二取非重入锁 = 永久自死锁**：进程不再响应、也没有任何异常可捕获。

    实测复现：`tests/test_auth_service.py::TestLoginPassword::should_lock_after_5_failed_attempts`
    走到「触达锁定阈值 → `notify_user_banned_committed` → `new_session()`」这条**首次**建会话
    的路径时整体挂死（faulthandler 栈停在 `session.py:58 with _engine_lock`）。
    """
    global _async_engine
    if _async_engine is None:
        _async_engine = create_realm_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            pool_max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=settings.db_pool_pre_ping,
        )
    return _async_engine


def get_async_engine() -> AsyncEngine:
    if _async_engine is None:
        # 双检锁：sync 依赖可能跑在 FastAPI 的线程池里，「先查后建」非原子会让两个线程各建
        # 一个引擎——败者被覆盖后永不 dispose，其连接池就这么泄漏（dispose_engine 只能清最后一个）
        with _engine_lock:
            _ensure_engine_locked()
    return _async_engine


def _get_async_session_local() -> async_sessionmaker[AsyncSession]:
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        with _engine_lock:  # 同上：与引擎共用一把锁，保证两者成对且只建一次
            if _AsyncSessionLocal is None:
                _AsyncSessionLocal = async_sessionmaker(
                    autocommit=False,
                    autoflush=False,
                    # 用 _ensure_engine_locked 而非 get_async_engine：此处已持锁，
                    # 再取一次会自死锁（见该函数注释）
                    bind=_ensure_engine_locked(),
                    expire_on_commit=False,
                )
    return _AsyncSessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供异步会话，负责 commit / rollback / close。"""
    db = _get_async_session_local()()
    try:
        yield db
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_unique_violation(exc):
            raise BizError(
                AuthErr.ALREADY_REGISTERED, "Resource already exists"
            ) from None
        raise
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


logger = logging.getLogger("lkm.db.session")


async def get_read_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：只读会话，供公开只读接口使用，避免每读请求一次空 BEGIN/COMMIT。

    不做 commit（读路径本无写入）；正常退出也显式 rollback 丢弃解析器误写/残留的
    未提交改动（防御某解析器意外 flush），异常同样回滚，最后 close。
    """
    db = _get_async_session_local()()
    try:
        yield db
        # 正常路径：只读，无提交意图；显式回滚以防解析器意外写入被残留到下一次。
        # 但「只读会话被写脏」是数据完整性 bug 的征兆，静默丢弃会让它永久不可见：先告警
        if db.new or db.dirty or db.deleted:
            logger.warning(
                "get_read_session 检测到未提交写入（new=%d dirty=%d deleted=%d），已丢弃",
                len(db.new),
                len(db.dirty),
                len(db.deleted),
            )
        await db.rollback()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def new_session() -> AsyncSession:
    """创建独立异步会话，与主会话共享同一引擎（连接池）但独立事务。"""
    return _get_async_session_local()()


async def dispose_engine() -> None:
    global _async_engine, _AsyncSessionLocal
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _AsyncSessionLocal = None


def _is_unique_violation(exc: IntegrityError) -> bool:
    """判断 IntegrityError 是否由唯一约束冲突引起（区别于外键/NOT NULL 等）。

    优先用 PostgreSQL SQLSTATE（唯一约束冲突恒为 23505；asyncpg 暴露 ``sqlstate``、
    psycopg 暴露 ``pgcode``，跨驱动稳定），类名匹配仅作兜底——驱动改名/包装就会静默误判，
    而本助手与 auth/db/session.py 共用，一次误判会同时影响两个库的错误映射。
    """
    orig = exc.orig
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is not None:
        return str(sqlstate) == _UNIQUE_VIOLATION_SQLSTATE
    return "UniqueViolation" in type(orig).__name__
