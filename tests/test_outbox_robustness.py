"""M6.3 outbox 健壮性补全验收：SKIP LOCKED / 认领标记与陈旧锁重置 / 失败分类 / 已发布归档。

与 ``test_outbox.py``（M1.1 语义）互补：这里只测本轮新增的四项能力，且断言的是**本轮新增
不变量**——别的 poller 已认领的行不被抢、崩溃进程留下的陈旧锁能被接管、永久失败不吃重试
额度、归档只动「已发布且超期」的行。绝不触真 Pulsar/Redis（``messaging.publish`` 内存替身）。

并发用 ``NullPool`` 引擎（每会话各自连接）——``test_outbox.py`` 的 StaticPool 单连接无法
模拟「两个事务同时持锁」，而 SKIP LOCKED 的正确性只在真并发下可见（无它则阻塞等待）。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.db.outbox  # noqa: F401  # 确保 OutboxMessage 入 Base.metadata
from app.core import messaging, outbox_relay
from app.core.config import settings
from app.db.base import Base
from app.db.event_failure import EventFailure
from app.db.model_registry import ensure_all_models
from app.db.outbox import OUTBOX_PENDING, OUTBOX_PUBLISHED, OutboxMessage
from app.db.outbox_archive import OutboxArchived

_RK = "event.apply_point"
_PAYLOAD = {"fn": "apply_point_event", "args": [7, "post", "item:9"]}
_SCHEMA = "s_outbox_rb"


@pytest.fixture
async def engine() -> AsyncEngine:
    """隔离 PG schema（业务库）：NullPool 让每会话独占连接，供真并发锁语义测试。"""
    ensure_all_models()
    url = settings.database_url
    boot = create_async_engine(url)
    async with boot.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
        await conn.execute(text(f'SET search_path TO "{_SCHEMA}"'))
        await conn.run_sync(Base.metadata.create_all)
    await boot.dispose()

    eng = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    yield eng
    await eng.dispose()
    clean = create_async_engine(url, poolclass=NullPool)
    try:
        async with clean.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    finally:
        await clean.dispose()


@pytest.fixture
async def fact(engine: AsyncEngine):
    maker = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )

    async def _new() -> AsyncSession:
        return maker()

    return _new


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://rb:6650")


async def _seed(fact, **kw) -> None:
    """落一行 outbox（默认 pending/就绪），可覆盖任意列。"""
    db = await fact()
    try:
        db.add(
            OutboxMessage(
                event_id=kw.pop("event_id", "evt-1"),
                routing_key=kw.pop("routing_key", _RK),
                payload_json=kw.pop("payload_json", _PAYLOAD),
                status=kw.pop("status", OUTBOX_PENDING),
                **kw,
            )
        )
        await db.commit()
    finally:
        await db.close()


async def _rows(fact) -> list[OutboxMessage]:
    db = await fact()
    try:
        return list((await db.execute(select(OutboxMessage))).scalars().all())
    finally:
        await db.close()


# ───────────────── 1) 认领标记：新鲜锁挡住别的 poller；陈旧锁可接管 ─────────────────


async def test_fresh_claim_lock_blocks_other_poller(fact, monkeypatch) -> None:
    """别的进程刚认领（locked_at 新鲜）→ 本 poller 不领、不投。"""
    calls: list[str] = []

    async def _pub(rk: str, payload: dict) -> bool:
        calls.append(rk)
        return True

    monkeypatch.setattr(messaging, "publish", _pub)
    await _seed(fact, locked_at=datetime.now(UTC), locked_by="other:999")

    assert await outbox_relay.relay_poll(session_factory=fact) == 0
    assert calls == []  # 未被抢投
    rows = await _rows(fact)
    assert rows[0].status == OUTBOX_PENDING
    assert rows[0].locked_by == "other:999"  # 认领者未被改写


async def test_stale_lock_is_reclaimed(fact, monkeypatch) -> None:
    """持锁进程崩溃（locked_at 早于 TTL）→ 该行可被重新领取并投出，锁标记清除。"""
    async def _pub(_rk: str, _payload: dict) -> bool:
        return True

    monkeypatch.setattr(messaging, "publish", _pub)
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.outbox_lock_ttl_s + 30
    )
    await _seed(fact, locked_at=stale, locked_by="dead:1")

    assert await outbox_relay.relay_poll(session_factory=fact) == 1
    row = (await _rows(fact))[0]
    assert row.status == OUTBOX_PUBLISHED
    assert row.locked_at is None and row.locked_by is None


async def test_skip_locked_does_not_block_on_concurrently_locked_row(
    fact, engine, monkeypatch
) -> None:
    """行锁被别的事务持有时 → SKIP LOCKED 跳过而非阻塞等待（否则本用例会挂住）。"""
    async def _pub(_rk: str, _payload: dict) -> bool:
        raise AssertionError("不应投递被别的事务锁住的行")

    monkeypatch.setattr(messaging, "publish", _pub)
    await _seed(fact)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    holder = maker()
    try:
        # 另开事务把该行 FOR UPDATE 锁住且不提交（模拟并发 poller 正领取）
        await holder.execute(
            select(OutboxMessage).with_for_update()
        )
        result = await asyncio.wait_for(
            outbox_relay.relay_poll(session_factory=fact), timeout=10
        )
        assert result == 0  # 跳过而非阻塞
    finally:
        await holder.rollback()
        await holder.close()


async def test_lock_cleared_after_retryable_failure(fact, monkeypatch) -> None:
    """瞬时失败 → 退避待重试，且认领标记已清（下轮可重新领取）。"""
    async def _fail(_rk: str, _payload: dict) -> bool:
        return False

    monkeypatch.setattr(messaging, "publish", _fail)
    await _seed(fact)

    assert await outbox_relay.relay_poll(session_factory=fact) == 0
    row = (await _rows(fact))[0]
    assert row.status == OUTBOX_PENDING
    assert row.attempt_count == 1
    assert row.locked_at is None and row.locked_by is None


# ───────────────────────── 2) 失败分类：永久失败不吃重试额度 ─────────────────────────


def test_permanent_failure_reason_classification() -> None:
    """分类器只看确定性错误：未知 routing_key / 不可 JSON 编码；有效事件返回 None。"""
    assert messaging.permanent_failure_reason(_RK, _PAYLOAD) is None
    unknown = messaging.permanent_failure_reason("bogus.topic", _PAYLOAD)
    assert unknown is not None and "unknown routing_key" in unknown
    # set 不是 JSON 可序列化对象 → 永久失败（线上编码路径直接抛，重试无意义）
    bad = messaging.permanent_failure_reason(_RK, {"fn": "x", "args": {1, 2}})
    assert bad is not None and "json-encodable" in bad


async def test_unknown_routing_key_folds_without_consuming_retries(
    fact, monkeypatch
) -> None:
    """未知 routing_key：一次即折叠进 event_failures，attempt_count 不累加、不重试。"""
    published: list[str] = []

    async def _pub(rk: str, _payload: dict) -> bool:
        published.append(rk)
        return True

    monkeypatch.setattr(messaging, "publish", _pub)
    await _seed(fact, event_id="bad-rk", routing_key="bogus.topic", attempt_count=2)

    assert await outbox_relay.relay_poll(session_factory=fact) == 0
    assert published == []  # 根本不进投递路径

    db = await fact()
    try:
        assert (await db.execute(select(OutboxMessage))).scalars().all() == []
        ef = (await db.execute(select(EventFailure))).scalars().one()
    finally:
        await db.close()
    assert ef.event_id == "bad-rk"
    assert ef.attempt_count == 2  # 未累加（区别于「重试耗竭」的 MAX_TRIES）
    assert "unknown routing_key" in ef.reason


# ───────────────────────── 3) published 归档（先归档后删） ─────────────────────────


async def test_archive_moves_only_expired_published_rows(fact) -> None:
    """归档只动「已发布且超保留期」的行：新鲜 published 与 pending 原样保留。"""
    old = datetime.now(UTC) - timedelta(hours=2)
    await _seed(fact, event_id="expired", status=OUTBOX_PUBLISHED, published_at=old)
    await _seed(
        fact,
        event_id="fresh",
        status=OUTBOX_PUBLISHED,
        published_at=datetime.now(UTC),
    )
    await _seed(fact, event_id="still-pending")

    assert await outbox_relay.archive_published(retention_s=3600, session_factory=fact) == 1

    db = await fact()
    try:
        left = {r.event_id for r in (await db.execute(select(OutboxMessage))).scalars()}
        arch = {r.event_id for r in (await db.execute(select(OutboxArchived))).scalars()}
    finally:
        await db.close()
    assert left == {"fresh", "still-pending"}  # 未超期/未发布的都不动
    assert arch == {"expired"}  # 先归档：冷副本在


async def test_archive_respects_batch_limit(fact) -> None:
    """单轮归档以批上限为界（表膨胀时不让一次动作拖长）。"""
    old = datetime.now(UTC) - timedelta(days=8)
    for i in range(3):
        await _seed(
            fact,
            event_id=f"e{i}",
            status=OUTBOX_PUBLISHED,
            published_at=old,
        )

    assert await outbox_relay.archive_published(
        retention_s=3600, batch=2, session_factory=fact
    ) == 2
    db = await fact()
    try:
        remaining = int(
            (
                await db.execute(select(func.count()).select_from(OutboxMessage))
            ).scalar_one()
        )
        archived = int(
            (
                await db.execute(select(func.count()).select_from(OutboxArchived))
            ).scalar_one()
        )
    finally:
        await db.close()
    assert remaining == 1 and archived == 2


async def test_archive_no_candidates_returns_zero(fact) -> None:
    await _seed(fact, event_id="pending-only")
    assert await outbox_relay.archive_published(session_factory=fact) == 0
