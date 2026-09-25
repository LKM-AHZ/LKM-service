"""模块5 优雅启停：lifespan 启动建资源、退出释放（init_db / redis / engine / 清理任务）。

启动不阻塞（蓝图 §2 第 1 条）后 schema 初始化移到后台 task：本文件既覆盖「后台正常跑完」，
也覆盖「初始化长期挂住时收尾能把它取消、其余资源照常释放」。
"""

import asyncio

import app.main as main_mod


def _patch_common(monkeypatch) -> list[str]:
    """把健康/清理类副作用替掉，并把调用记录进返回的 list。

    lifespan 还会真实调用 user_cache_events.start / messaging.shutdown / clickhouse.close
    等，这些在无 Redis/无 Pulsar 的单测环境本就是 no-op 或退避空转（既有用例即如此）。
    """
    calls: list[str] = []

    async def _fake_get_redis() -> None:
        calls.append("redis_probe")

    async def _fake_close_redis() -> None:
        calls.append("close_redis")

    async def _fake_dispose_engine() -> None:
        calls.append("dispose_engine")

    async def _fake_cleanup() -> None:
        calls.append("cleanup_start")

    monkeypatch.setattr(main_mod.redis_client, "get_redis", _fake_get_redis)
    monkeypatch.setattr(main_mod.redis_client, "close_redis", _fake_close_redis)
    monkeypatch.setattr(main_mod, "dispose_engine", _fake_dispose_engine)
    monkeypatch.setattr(main_mod, "cleanup_expired_challenges", _fake_cleanup)
    return calls


async def test_lifespan_runs_setup_and_graceful_cleanup(monkeypatch) -> None:
    """进入 lifespan 建资源；yield 后依次释放 redis/engine 并取消后台 task。"""

    calls = _patch_common(monkeypatch)

    async def _fake_init_db() -> None:
        calls.append("init_db")

    monkeypatch.setattr(main_mod, "init_db", _fake_init_db)

    entered = False
    async with main_mod.lifespan(None):  # ty: ignore[invalid-argument-type]  # 故意传 None 单测 lifespan
        entered = True
        # 让后台 schema 初始化 task 拿到一次调度机会。生产里它启动后自行跑完；此处显式让出，
        # 否则收尾的 cancel 可能在它首次运行前就把它取消，断言依赖调度时序。
        await asyncio.sleep(0)

    assert entered
    assert "init_db" in calls  # 后台执行 schema 初始化
    assert "redis_probe" in calls  # 启动探测 Redis
    assert "close_redis" in calls  # 退出释放 Redis
    assert "dispose_engine" in calls  # 退出释放引擎


async def test_lifespan_cancels_hanging_init_db_and_still_releases(monkeypatch) -> None:
    """schema 初始化长期挂住（DB 不可用）时：进程照常起，收尾取消该 task 且其余资源照常释放。

    这是「启动不阻塞」的底线——不取消它，后台 task 会在引擎 dispose 之后继续用连接池。
    """

    calls = _patch_common(monkeypatch)
    init_started = asyncio.Event()

    async def _hanging_init_db() -> None:
        calls.append("init_db_start")
        init_started.set()
        await asyncio.Event().wait()  # 永不返回，模拟 DB 长期不可达

    monkeypatch.setattr(main_mod, "init_db", _hanging_init_db)

    async with main_mod.lifespan(None):  # ty: ignore[invalid-argument-type]
        # 启动不被 init_db 阻塞：进得来，且后台确实在跑（而不是 await 住）
        await asyncio.wait_for(init_started.wait(), timeout=5)

    assert "init_db_start" in calls
    assert "close_redis" in calls  # 挂住的初始化没有拖住收尾
    assert "dispose_engine" in calls
