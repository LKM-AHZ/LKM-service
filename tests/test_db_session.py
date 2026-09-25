"""db/session.py 的 IntegrityError 映射单测：唯一约束 vs 其他，及引擎池策略。"""

from typing import Any

from sqlalchemy.exc import IntegrityError

from app.db.session import _is_unique_violation, get_async_engine


def _ie(orig: Exception | None) -> IntegrityError:
    # IntegrityError.orig 的 pyi 标非 None，但 SQLAlchemy 运行时允许 orig=None
    return IntegrityError("stmt", {}, orig)  # ty: ignore[invalid-argument-type]


class TestIsUniqueViolation:
    """纯 PostgreSQL 语义：只靠 asyncpg 错误类名（UniqueViolation）判定。"""

    def should_detect_unique_violation_by_class_name(self):
        class UniqueViolation(Exception):
            pass

        assert _is_unique_violation(_ie(UniqueViolation("dup"))) is True

    def should_not_detect_other_violation_by_class_name(self):
        # 真 PG 下的外键违例类名是 ForeignKeyViolation（非 UniqueViolation）
        class ForeignKeyViolation(Exception):
            pass

        class NotNullViolation(Exception):
            pass

        assert _is_unique_violation(_ie(ForeignKeyViolation("fk"))) is False
        assert _is_unique_violation(_ie(NotNullViolation("nn"))) is False

    def should_return_false_when_no_orig(self):
        assert _is_unique_violation(_ie(None)) is False


class TestEnginePoolConfig:
    """PostgreSQL(asyncpg) 建池：显式 pool_size/max_overflow/pool_pre_ping。"""

    def should_configure_pool_for_postgres(self, monkeypatch):
        import app.db.session as session_mod
        from app.core.config import settings

        captured: dict[str, Any] = {}

        def _fake_create(url: str, **kwargs: Any) -> Any:
            captured.update(kwargs)

            class _Shell:
                sync_engine: Any = None

            return _Shell()

        monkeypatch.setattr(session_mod, "create_async_engine", _fake_create)
        monkeypatch.setattr(session_mod, "_async_engine", None)
        monkeypatch.setattr(session_mod, "_AsyncSessionLocal", None)

        get_async_engine()
        assert captured.get("pool_size") == settings.db_pool_size
        assert captured.get("max_overflow") == settings.db_pool_max_overflow
        assert captured.get("pool_pre_ping") is True

    def should_configure_worker_pool_separately(self, monkeypatch):
        """worker 池是**独立引擎**且用 worker 专用池参数（蓝图 §3.3「独立连接池」）。

        分池的意义：outbox relay / 调度任务 / worker 的周期突发不再与 Web 请求争抢连接。
        """
        import app.db.session as session_mod
        from app.core.config import settings
        from app.db.session import get_async_engine, get_worker_engine

        calls: list[dict[str, Any]] = []

        def _fake_create(url: str, **kwargs: Any) -> Any:
            calls.append(kwargs)

            class _Shell:
                sync_engine: Any = None

            return _Shell()

        monkeypatch.setattr(session_mod, "create_async_engine", _fake_create)
        monkeypatch.setattr(session_mod, "_async_engine", None)
        monkeypatch.setattr(session_mod, "_AsyncSessionLocal", None)
        monkeypatch.setattr(session_mod, "_worker_engine", None)
        monkeypatch.setattr(session_mod, "_WorkerSessionLocal", None)

        main_engine = get_async_engine()
        worker_engine = get_worker_engine()
        assert worker_engine is not main_engine  # 关键：不同实例才叫分池
        assert calls[0]["pool_size"] == settings.db_pool_size
        assert calls[1]["pool_size"] == settings.db_worker_pool_size
        assert calls[1]["max_overflow"] == settings.db_worker_pool_max_overflow


class TestReadSession:
    """读会话 exit 后不自动 commit（只读请求省空事务）。"""

    async def should_not_commit_on_exit(self, monkeypatch) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.pool import StaticPool

        import app.db.session as session_mod
        from app.core.config import settings

        engine = create_async_engine(settings.database_url, poolclass=StaticPool)

        calls: list[str] = []

        class _TrackingSession(AsyncSession):
            async def commit(self) -> None:
                calls.append("commit")
                await super().commit()

        factory = session_mod.async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            class_=_TrackingSession,
        )
        monkeypatch.setattr(session_mod, "_AsyncSessionLocal", factory)

        async for _db in session_mod.get_read_session():
            pass  # 仅走完整生命周期：进入 + 退出

        assert calls == [], "读会话退出不应触发 commit"


class TestLazySingletonNoSelfDeadlock:
    """冷启动下 `_get_async_session_local()` 不得自死锁。

    回归点：它曾在**持有 `_engine_lock` 时**调用 `get_async_engine()` 来绑定引擎，而
    `_engine_lock` 是 `threading.Lock`（非重入）。当 `_async_engine` 恰为 `None`
    （冷启动，或 `dispose_engine()` 之后），`get_async_engine` 会再取同一把锁——
    **同一线程二取非重入锁 = 永久自死锁**：没有异常、没有日志，进程只是不再响应。

    实测原始症状：`tests/test_auth_service.py::TestLoginPassword::should_lock_after_5_failed_attempts`
    走到「触达锁定阈值 → `auth.events.notify_user_banned_committed` → `new_session()`」这条
    **首次**建会话的路径时整体挂死（faulthandler 栈停在 `session.py` 的 `with _engine_lock`）。

    用线程 + `join(timeout)` 跑：把「永久挂起」变成一条可读的断言失败，而不是让整个 pytest
    卡死到超时（当时的表现正是后者，排查成本极高）。
    """

    def should_build_session_local_on_cold_start(self, monkeypatch) -> None:
        import threading

        import app.db.session as session_mod

        assert not session_mod._engine_lock.locked(), "前置用例泄漏了 _engine_lock"
        monkeypatch.setattr(session_mod, "_async_engine", None)
        monkeypatch.setattr(session_mod, "_AsyncSessionLocal", None)

        box: dict[str, Any] = {}

        def _run() -> None:
            try:
                box["ok"] = session_mod._get_async_session_local()
            except BaseException as exc:
                box["err"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=10)

        assert not worker.is_alive(), (
            "冷启动建会话自死锁：_get_async_session_local 在持 _engine_lock 时又取了同一把锁"
        )
        assert "err" not in box, f"冷启动建会话抛错：{box.get('err')!r}"
        assert box.get("ok") is not None
