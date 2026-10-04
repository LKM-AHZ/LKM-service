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

from core.config import settings
from core.err import (
    BizError,
    CommonErr,
    ErrCode,
    unique_constraint_errcode,
)

_async_engine: AsyncEngine | None = None
_AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None
# worker / 后台批处理的独立引擎与会话工厂（蓝图 §3.3「不同组件独立连接池」标"关键"）：
# Web 请求、批处理各自持池，outbox relay/Prefect/worker 的周期突发不会把在线请求的
# 连接挤干。两者共用同一把 _engine_lock（成对创建，见下）。
_worker_engine: AsyncEngine | None = None
_WorkerSessionLocal: async_sessionmaker[AsyncSession] | None = None
_engine_lock = threading.Lock()
# PostgreSQL SQLSTATE：唯一约束冲突
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def create_realm_async_engine(
    url: str,
    *,
    pool_size: int | None = None,
    pool_max_overflow: int | None = None,
    pool_pre_ping: bool | None = None,
    pool_recycle: int | None = None,
    pool_timeout: float | None = None,
) -> AsyncEngine:
    """按池参数建立 PostgreSQL(asyncpg) async 引擎。

    供主库（:func:`get_async_engine`）与 auth 独立库（auth/db/session.py）共用的唯一
    建池逻辑，避免两处策略漂移。

    ``pool_recycle``/``pool_timeout`` 见 ``settings.db_pool_recycle_s``/``db_pool_timeout_s``
    的注释（蓝图 §3.3：连接回收 + 池满等待超时）。两者是**池形态**参数，各 realm 共用；
    只有尺寸（size/overflow）按 realm 分列。
    """
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": False, "connect_args": connect_args}
    if pool_size is not None:
        engine_kwargs["pool_size"] = pool_size
    if pool_max_overflow is not None:
        engine_kwargs["max_overflow"] = pool_max_overflow
    if pool_pre_ping is not None:
        engine_kwargs["pool_pre_ping"] = pool_pre_ping
    if pool_recycle is not None:
        engine_kwargs["pool_recycle"] = pool_recycle
    if pool_timeout is not None:
        engine_kwargs["pool_timeout"] = pool_timeout
    return create_async_engine(url, **engine_kwargs)


def _ensure_engine_locked() -> AsyncEngine:
    """**调用方必须已持有 `_engine_lock`** 时使用；返回惰性单例引擎。

    存在的唯一理由：`_get_async_session_local` 要在持锁状态下绑定引擎，而 `_engine_lock`
    是 `threading.Lock`（**非重入**）。它若在那里直接调 `get_async_engine()`，而 `_async_engine`
    恰为 None（冷启动，或 `dispose_engine()` 之后），`get_async_engine` 会再取同一把锁
    ——**同一个线程二取非重入锁 = 永久自死锁**：进程不再响应、也没有任何异常可捕获。

    实测复现：`tests/test_auth_service.py::TestLoginPassword::should_lock_after_5_failed_attempts`
    走到「触达锁定阈值 → `notify_user_banned` → `new_session()`」这条**首次**建会话
    的路径时整体挂死（faulthandler 栈停在 `session.py:58 with _engine_lock`）。
    """
    global _async_engine
    if _async_engine is None:
        _async_engine = create_realm_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            pool_max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=settings.db_pool_pre_ping,
            pool_recycle=settings.db_pool_recycle_s,
            pool_timeout=settings.db_pool_timeout_s,
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
            # 冲突一律 409；具体文案随登记的语义码，未登记的回落到中性的 CommonErr.CONFLICT
            # （不再是 auth 域的 "Username or email already registered"，见该码注释）。
            raise BizError(unique_violation_errcode(exc)) from None
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
    """创建独立异步会话，与主会话共享同一引擎（连接池）但独立事务。

    **Web 请求路径用这个**。后台批处理（worker / outbox relay / 调度任务 / flow）请用
    :func:`new_worker_session`——它们走独立池，不与在线请求争抢连接。
    """
    return _get_async_session_local()()


def _ensure_worker_engine_locked() -> AsyncEngine:
    """**调用方必须已持有 `_engine_lock`**；返回 worker 池的惰性单例引擎。

    与 :func:`_ensure_engine_locked` 同因：`_engine_lock` 是非重入 `threading.Lock`，
    持锁状态下不能再调 ``get_worker_engine()``（会自死锁）。
    """
    global _worker_engine
    if _worker_engine is None:
        _worker_engine = create_realm_async_engine(
            settings.database_url,
            pool_size=settings.db_worker_pool_size,
            pool_max_overflow=settings.db_worker_pool_max_overflow,
            pool_pre_ping=settings.db_pool_pre_ping,
            pool_recycle=settings.db_pool_recycle_s,
            pool_timeout=settings.db_pool_timeout_s,
        )
    return _worker_engine


def get_worker_engine() -> AsyncEngine:
    """worker 池引擎（与 Web 主池**不同实例**，池参数独立）。"""
    if _worker_engine is None:
        # 双检锁同 get_async_engine：sync 依赖可能来自线程池
        with _engine_lock:
            _ensure_worker_engine_locked()
    return _worker_engine


def _get_worker_session_local() -> async_sessionmaker[AsyncSession]:
    global _WorkerSessionLocal
    if _WorkerSessionLocal is None:
        with _engine_lock:  # 与 worker 引擎共用同一把锁，保证成对且只建一次
            if _WorkerSessionLocal is None:
                _WorkerSessionLocal = async_sessionmaker(
                    autocommit=False,
                    autoflush=False,
                    bind=_ensure_worker_engine_locked(),
                    expire_on_commit=False,
                )
    return _WorkerSessionLocal


async def new_worker_session() -> AsyncSession:
    """创建**后台批处理**用的独立会话（独立连接池，不与 Web 请求争抢）。

    语义与 :func:`new_session` 相同（调用方自行 commit/close），差别只在池。改用的调用点
    建议以别名导入保持模块属性名不变（``from core.db.session import new_worker_session as
    new_session``），这样测试对 ``new_session`` 的 monkeypatch 缝依旧生效。
    """
    return _get_worker_session_local()()


async def dispose_engine() -> None:
    global _async_engine, _AsyncSessionLocal, _worker_engine, _WorkerSessionLocal
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _AsyncSessionLocal = None
    if _worker_engine is not None:
        await _worker_engine.dispose()
        _worker_engine = None
        _WorkerSessionLocal = None


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


def _unique_constraint_name(exc: IntegrityError) -> str | None:
    """取唯一约束名，取不到返回 None（蓝图 §6.1 语义化映射的输入）。

    坑在「谁身上才有约束名」：SQLAlchemy 的 asyncpg 方言把自己的
    ``sqlalchemy.dialects.postgresql.asyncpg.UniqueViolationError`` 放在 ``exc.orig``，
    它**只透出 sqlstate/pgcode**，约束名在它包的驱动异常上（``.driver_exception`` /
    ``.orig`` / ``__cause__``，实测三者同一对象）。故按「先直接、再解包」的顺序找；
    psycopg 则在 ``.diag.constraint_name``。都取不到就回落，交给调用方走现状兜底码。
    """
    orig = exc.orig
    if orig is None:
        return None
    candidates = (
        orig,
        getattr(orig, "driver_exception", None),
        getattr(orig, "orig", None),
        getattr(orig, "__cause__", None),
    )
    for cand in candidates:
        if cand is None:
            continue
        name = getattr(cand, "constraint_name", None)
        if name:
            return str(name)
        diag = getattr(cand, "diag", None)
        name = getattr(diag, "constraint_name", None) if diag is not None else None
        if name:
            return str(name)
    return None


def unique_violation_errcode(exc: IntegrityError) -> ErrCode:
    """按约束名给唯一冲突选语义化错误码；未命中回落中性的 ``CommonErr.CONFLICT``(409)。

    映射**不在此硬编码**：约束名跨环境会变（create_all 隐式命名如 ``content_likes_pkey``
    vs alembic 显式命名），故由各模块在自身 ``errors.py`` 用
    :func:`core.err.register_unique_constraint` 登记片段，本函数只查 core 的注册表
    ——db 层不反向 import 业务模块（import-linter 的「db 不依赖 modules」契约）。

    兜底刻意**不用** ``AuthErr.ALREADY_REGISTERED``：那是注册入口的语义码，用在业务表冲突上
    会把「重复收藏/重复申请」说成「用户名或邮箱已注册」（状态码对、文案误导）。
    """
    return unique_constraint_errcode(_unique_constraint_name(exc)) or CommonErr.CONFLICT
