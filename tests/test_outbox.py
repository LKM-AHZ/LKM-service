"""M1.1 outbox 事务发件箱 + relay 验收。

enqueue（业务会话）与 relay（session_factory 注入同一 engine）读写同一 PG 隔离 schema，
避免 relay 默认走生产 new_session 而连到别的库、读不到内存行。队列侧（worker 消费幂等）同样
经 monkeypatch new_session 走同一库。
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

import app.db.outbox  # noqa: F401  # 确保 OutboxMessage 已入 Base.metadata
from app.core import messaging, outbox_relay, worker
from app.core.config import settings
from app.db.base import Base
from app.db.event_processed import EventProcessed, already_processed, record_processed
from app.db.model_registry import ensure_all_models
from app.db.outbox import (
    MAX_TRIES,
    OUTBOX_PENDING,
    OUTBOX_PUBLISHED,
    OutboxMessage,
    enqueue_outbox,
)

_RK = "event.apply_point"
_PAYLOAD = {"fn": "apply_point_event", "args": [7, "post", "item:9"]}


@pytest.fixture
async def engine() -> AsyncEngine:
    """隔离 PG schema（业务库）：全量 ensure 模型 + set search_path 后 create_all 落此。"""
    ensure_all_models()
    url = settings.database_url
    schema = "s_outbox"
    eng = create_async_engine(url, poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    # 测毕清理一次性 schema
    _clean = create_async_engine(url, poolclass=NullPool)
    try:
        async with _clean.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await _clean.dispose()


@pytest.fixture
async def fact(engine: AsyncEngine):
    """返回会话工厂：业务(enqueue/add) 与 relay session_factory 共用此引擎。"""
    maker = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )

    async def _new() -> AsyncSession:
        # 同引擎（StaticPool 已 SET search_path）→ 新会话仍落该 schema
        return maker()

    return _new


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch) -> None:
    """本文件默认当“配置了消息总线”，放行 enqueue gate。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://ci:6650")


async def _seed_one(fact, **row_kw) -> None:
    """enqueue 一条事件并提交。"""
    db = await fact()
    try:
        await enqueue_outbox(
            db,
            row_kw.pop("routing_key", _RK),
            row_kw.pop("payload", _PAYLOAD),
            event_id=None,
        )
        await db.commit()
    finally:
        await db.close()


async def _row(fact):
    db = await fact()
    try:
        return (await db.execute(sa.select(OutboxMessage))).scalars().one()
    finally:
        await db.close()


async def test_enqueue_persists_then_relay_publishes(fact, monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []

    async def _pub(rk: str, payload: dict) -> bool:
        sent.append((rk, dict(payload)))
        return True

    monkeypatch.setattr(messaging, "publish", _pub)

    await _seed_one(fact)
    done = await outbox_relay.relay_poll(session_factory=fact)

    assert done == 1
    assert len(sent) == 1
    rk, payload = sent[0]
    assert rk == _RK
    # M1.3：relay 发布时把 outbox event_id 透传为消费端幂等键；业务字段原样保留。
    assert payload["event_id"]
    payload.pop("event_id")
    assert payload == _PAYLOAD
    row = await _row(fact)
    assert row.status == OUTBOX_PUBLISHED
    assert row.published_at is not None


async def test_enqueue_skips_when_bus_blank(fact, monkeypatch) -> None:
    monkeypatch.setattr(settings, "pulsar_url", "")
    db = await fact()
    try:
        assert await enqueue_outbox(db, _RK, _PAYLOAD) is False
        await db.commit()
    finally:
        await db.close()

    db2 = await fact()
    try:
        rows = (await db2.execute(sa.select(OutboxMessage))).scalars().all()
        assert rows == []
    finally:
        await db2.close()


async def test_enqueue_idempotent_by_event_id(fact) -> None:
    # 第一次加入并提交
    db = await fact()
    try:
        assert await enqueue_outbox(db, _RK, _PAYLOAD, event_id="dup-evt-1") is True
        await db.commit()
    finally:
        await db.close()

    # 第二次（新事务）同 event_id、仍 pending：跳过，不产生第二行
    db2 = await fact()
    try:
        assert await enqueue_outbox(db2, _RK, _PAYLOAD, event_id="dup-evt-1") is False
        await db2.commit()
        rows = (await db2.execute(sa.select(OutboxMessage))).scalars().all()
        assert len(rows) == 1
    finally:
        await db2.close()


async def test_relay_failure_backoff_then_recovery(fact, monkeypatch) -> None:
    calls = {"n": 0}

    async def _pub(_rk: str, _payload: dict) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # 第 1 次失败、第 2 次成功

    monkeypatch.setattr(messaging, "publish", _pub)

    await _seed_one(fact)
    first = await outbox_relay.relay_poll(session_factory=fact)
    assert first == 0  # 首次失败未发布

    # 把重试时间拨回过去，让下一轮立即可领
    db = await fact()
    try:
        row = (await db.execute(sa.select(OutboxMessage))).scalars().one()
        assert row.status == OUTBOX_PENDING
        assert row.attempt_count == 1
        assert row.next_retry_at > datetime.now(UTC)  # 已退避到将来
        row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    finally:
        await db.close()

    second = await outbox_relay.relay_poll(session_factory=fact)
    assert second == 1
    row = await _row(fact)
    assert row.status == OUTBOX_PUBLISHED
    assert row.attempt_count == 1  # 只投成功一次，无重复副作用


async def test_relay_folds_to_event_failure_at_max_tries(fact, monkeypatch) -> None:
    async def _fail(_rk: str, _payload: dict) -> bool:
        return False

    monkeypatch.setattr(messaging, "publish", _fail)

    # 预置 near-max（此刻即就绪）；一次失败后累到 MAX → 折叠归档进 event_failures，
    # 从 outbox_events 迁出（不再挤占后续轮 / pending 窗口 / 积压 gauge）。
    db = await fact()
    m = OutboxMessage(
        event_id="near-max",
        routing_key=_RK,
        payload_json=_PAYLOAD,
        status=OUTBOX_PENDING,
        attempt_count=MAX_TRIES - 2,
    )
    try:
        db.add(m)
        await db.commit()
        # 逐轮失败（拨回 next_retry 就地即可领），直到 reaching max → fold
        for _ in range(2):
            row = (await db.execute(sa.select(OutboxMessage))).scalars().one()
            row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            await outbox_relay.relay_poll(session_factory=fact)
        # 原 outbox 行已迁出；归档表存一笔审计副本
        outbox_left = (await db.execute(sa.select(OutboxMessage))).scalars().all()
        assert outbox_left == []
        from app.db.event_failure import EventFailure

        ef = (await db.execute(sa.select(EventFailure))).scalars().one()
        assert ef.event_id == "near-max"
        assert ef.attempt_count == MAX_TRIES
        assert "relay exhausted" in ef.reason
    finally:
        await db.close()


def _pending_gauge() -> float:
    """读 outbox_pending_count 现值；未 set 视为 0（series 未出现过时不抛错）。"""
    val = REGISTRY.get_sample_value("outbox_pending_count")
    return val if val is not None else 0.0


async def test_relay_reports_zero_gauge_after_full_drain(fact, monkeypatch) -> None:
    # 全部投递成功 → relay 末尾 gauge = 0（表无剩余 pending）：积压看板不误报。
    async def _ok(_rk: str, _payload: dict) -> bool:
        return True

    monkeypatch.setattr(messaging, "publish", _ok)

    await _seed_one(fact)
    done = await outbox_relay.relay_poll(session_factory=fact)
    assert done == 1
    assert _pending_gauge() == 0


async def test_relay_reports_backlog_when_event_remains_pending(
    fact, monkeypatch
) -> None:
    # 持续失败（未达 MAX）→ 事件留 pending 待退避 → relay 末尾 gauge = 剩余积压件数。
    async def _fail(_rk: str, _payload: dict) -> bool:
        return False

    monkeypatch.setattr(messaging, "publish", _fail)

    await _seed_one(fact)
    done = await outbox_relay.relay_poll(session_factory=fact)
    assert done == 0
    assert _pending_gauge() == 1


async def test_relay_folds_backlogged_event_at_max_tries(fact, monkeypatch) -> None:
    # 达 MAX fold 出 outbox（不再 pending）→ 同批不计入积压 gauge。
    async def _fail(_rk: str, _payload: dict) -> bool:
        return False

    monkeypatch.setattr(messaging, "publish", _fail)

    db = await fact()
    m = OutboxMessage(
        event_id="soon-failed",
        routing_key=_RK,
        payload_json=_PAYLOAD,
        status=OUTBOX_PENDING,
        attempt_count=MAX_TRIES - 1,
    )
    try:
        db.add(m)
        await db.commit()
        await outbox_relay.relay_poll(session_factory=fact)
        from app.db.event_failure import EventFailure

        ef = (await db.execute(sa.select(EventFailure))).scalars().one()
        assert ef.event_id == "soon-failed"
        assert ef.attempt_count == MAX_TRIES
        assert _pending_gauge() == 0
    finally:
        await db.close()


# ================= M1.3 消费者幂等（event_processed 账本 + relay 透传 + worker 去重）=====


async def test_record_processed_then_seen(fact) -> None:
    db = await fact()
    try:
        assert await already_processed(db, "evt-1") is False
        assert await record_processed(db, "evt-1") is True
        assert await already_processed(db, "evt-1") is True
    finally:
        await db.close()


async def test_record_processed_idempotent_nop(fact) -> None:
    db = await fact()
    try:
        await record_processed(db, "evt-dup")
        # 主键唯一约束：再记同 id 不抛、返回 False、表内仍只一行。
        assert await record_processed(db, "evt-dup") is False
        rows = (await db.execute(sa.select(EventProcessed))).scalars().all()
        assert len(rows) == 1
    finally:
        await db.close()


async def test_relay_payload_carries_event_id(fact, monkeypatch) -> None:
    """relay 发布 → 消息带 outbox 幂等键 event_id（供消费端去重），业务字段原样。"""
    sent: list[dict] = []

    async def _pub(_rk: str, payload: dict) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(messaging, "publish", _pub)
    db = await fact()
    try:
        await enqueue_outbox(db, "event.notify_upload", {"fn": "n", "args": ["u"]})
        await db.commit()
    finally:
        await db.close()
    await outbox_relay.relay_poll(session_factory=fact)
    assert len(sent) == 1
    assert sent[0]["event_id"]  # 透传
    assert sent[0]["fn"] == "n"


async def _dispatch(fact, monkeypatch, payload: dict, handler) -> None:
    """worker 的 new_session 换成内存库会话工厂 → 驱动 _dispatch_with_dedup 一次。"""

    async def _fake_new_session() -> AsyncSession:
        return await fact()

    monkeypatch.setattr(worker, "new_session", _fake_new_session)
    await worker._dispatch_with_dedup(payload, handler, payload.get("args", []))


async def test_dispatch_same_event_runs_handler_once(fact, monkeypatch) -> None:
    calls: list[str] = []

    async def h(upload_id: str) -> None:
        calls.append(upload_id)

    payload = {"fn": "n", "event_id": "dup-1", "args": ["up-1"]}
    await _dispatch(fact, monkeypatch, payload, h)
    # 第二次（重放 / DLQ requeue 同 event_id）→ 账本命中 → 跳过 handler。
    await _dispatch(fact, monkeypatch, payload, h)
    assert calls == ["up-1"]  # 只执行一次


async def test_dispatch_failure_skips_recording(fact, monkeypatch) -> None:
    async def boom(_upload_id: str) -> None:
        raise RuntimeError("boom")

    payload = {"fn": "n", "event_id": "evt-fail", "args": ["up-f"]}
    with pytest.raises(RuntimeError, match="boom"):
        await _dispatch(fact, monkeypatch, payload, boom)
    db = await fact()
    try:
        assert await already_processed(db, "evt-fail") is False  # 失败不记账,可重试
    finally:
        await db.close()


async def test_dispatch_no_event_id_still_runs(fact, monkeypatch) -> None:
    calls: list[str] = []

    async def h(x: str) -> None:
        calls.append(x)

    # 无 event_id（send/cron 直发）→ 不经账本就执行。
    await _dispatch(fact, monkeypatch, {"fn": "x", "args": ["c"]}, h)
    assert calls == ["c"]
