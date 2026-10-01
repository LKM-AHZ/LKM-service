"""
outbox relay：领取 pending 事件投递到消息总线并置 published。
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import WatchError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import event_contract, messaging
from core import redis as redis_client
from core.config import settings
from core.db.event_failure import EventFailure
from core.db.outbox import (
    _BACKOFF_CAP_S,
    MAX_TRIES,
    OUTBOX_PENDING,
    OUTBOX_PUBLISHED,
    OutboxMessage,
)
from core.db.outbox_archive import OutboxArchived
from core.db.session import new_worker_session as new_session
from core.metrics import outbox_leader_total, outbox_pending_count

logger = logging.getLogger("lkm.outbox")

# 会话工厂类型：relay_poll 允许单测注入独立内存库会话，默认走生产 async_session(new_session)
SessionFactory = Callable[..., Awaitable[AsyncSession]]

# 本进程标识，写入 `locked_by`（M6.3）：定位「某行被哪个进程认领」，也在排障时区分副本。
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"[:64]


def _scan_window_start(now: datetime) -> datetime | None:
    """
    扫描窗口的时间下界
    ``outbox_scan_window_s <= 0`` 时返回 None（不限窗）。它只用于让规划器跳过
    ``created_at`` 过旧的分区，**不改变「哪些事件可投」的语义**——窗口外的行会被
    Timescale 保留策略 DROP（两者阈值刻意对齐，见 ``init_db._RETENTION_POLICIES``）；
    普通 PG（无 hypertable）上开启它也无副作用，只是少扫陈年滞留行。
    """
    if settings.outbox_scan_window_s <= 0:
        return None
    return now - timedelta(seconds=settings.outbox_scan_window_s)


def _claimable(now: datetime) -> tuple[Any, ...]:
    """
    可领取窗口的 WHERE 条件：到期待投 **且** 未被别的进程有效认领
    锁列（`locked_at/locked_by`）此前只建不用；现在领取即写、投递后清，并叠加陈旧阈值：
    `locked_at` 比 TTL 更早的行视为「持有者已崩溃」，可被重新领取——否则持锁进程崩溃会让
    该行永久卡死。未到期（`locked_at` 新鲜）的行留给持有者，别的副本不抢。
    """
    stale_before = now - timedelta(
        seconds=max(settings.outbox_lock_ttl_s, settings.pulsar_operation_timeout_s + 10)
    )
    conds: list[Any] = [
        OutboxMessage.status == OUTBOX_PENDING,
        OutboxMessage.next_retry_at <= now,
        or_(
            OutboxMessage.locked_at.is_(None),
            OutboxMessage.locked_at < stale_before,
        ),
    ]
    window_start = _scan_window_start(now)
    if window_start is not None:
        conds.append(OutboxMessage.created_at >= window_start)
    return tuple(conds)


def _clear_lock(msg: OutboxMessage) -> None:
    """投递尝试结束（成功或失败）即清认领标记，令该行按自身退避窗口重新可领。"""
    msg.locked_at = None
    msg.locked_by = None


async def relay_poll(
    batch: int = 100,
    *,
    session_factory: SessionFactory | None = None,
    can_publish: Callable[[], bool] | None = None,
) -> int:
    """扫一批到期的 pending 事件投递；返回本轮成功(published)事件数。"""
    factory = session_factory or new_session
    db = await factory()
    succeeded = 0
    try:
        # 每次只领取一行。整批预领取会让末尾行在等待前面网络投递时过期，
        # 被另一副本接管并双投。单行领取还给每次投递单独的 fencing token。
        for _ in range(batch):
            if can_publish is not None and not can_publish():
                break
            msg = await db.scalar(
                select(OutboxMessage)
                .where(*_claimable(datetime.now(UTC)))
                .order_by(OutboxMessage.attempt_count.asc(), OutboxMessage.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if msg is None:
                break
            if msg.locked_at is not None:
                outbox_leader_total.labels("stale_reclaimed").inc()
            claim_id = f"{_INSTANCE_ID}:{uuid.uuid4().hex}"[-64:]
            msg.locked_at = datetime.now(UTC)
            msg.locked_by = claim_id
            await db.commit()
            if can_publish is not None and not can_publish():
                owned = await db.scalar(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.id == msg.id,
                        OutboxMessage.created_at == msg.created_at,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if owned is not None and owned.locked_by == claim_id:
                    _clear_lock(owned)
                    await db.commit()
                else:
                    await db.rollback()
                break

            # 把 outbox 幂等键 event_id 透传进发布消息，消费端据此按「已处理记账」去重
            payload = {**msg.payload_json, "event_id": msg.event_id}
            reason = messaging.permanent_failure_reason(msg.routing_key, payload)
            if reason is None:
                try:
                    ok = await asyncio.wait_for(
                        messaging.publish(msg.routing_key, payload),
                        timeout=min(
                            settings.pulsar_operation_timeout_s + 5,
                            max(0.1, settings.outbox_lock_ttl_s / 2),
                        ),
                    )
                except Exception:
                    logger.exception(
                        "outbox publish exception id=%s rk=%s", msg.id, msg.routing_key
                    )
                    ok = False
            else:
                ok = False

            # 发布期间若锁超时并被接管，旧持有者不得再覆盖新持有者的状态。
            msg_id = msg.id
            current = await db.scalar(
                select(OutboxMessage)
                .where(
                    OutboxMessage.id == msg.id,
                    OutboxMessage.created_at == msg.created_at,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is None or current.locked_by != claim_id:
                await db.rollback()
                logger.warning("outbox claim 已失效，跳过状态更新 id=%s", msg_id)
                continue
            msg = current
            if reason is not None:
                # 确定性错误：重试不会变好，一次即折叠（不累加 attempt_count，不空耗退避）。
                # 契约违约另记违约指标：这条路径**不会**走到 messaging.publish（在投递前就折叠了），
                # 不在这里记账的话「relay 折叠掉的违约」在 /metrics 上完全不可见。
                problems = messaging.contract_violations(msg.routing_key, payload)
                if problems:
                    event_contract.record_violation(
                        payload.get("fn"),
                        "produce",
                        problems,
                        where=f"outbox-relay rk={msg.routing_key}",
                    )
                logger.error(
                    "outbox 永久失败折叠 id=%s rk=%s reason=%s",
                    msg.id,
                    msg.routing_key,
                    reason,
                )
                db.add(
                    EventFailure(
                        event_id=msg.event_id,
                        routing_key=msg.routing_key,
                        payload_json=msg.payload_json,
                        attempt_count=msg.attempt_count,
                        reason=reason,
                    )
                )
                await db.delete(msg)
                await db.commit()
                continue

            _clear_lock(msg)
            if ok:
                msg.status = OUTBOX_PUBLISHED
                msg.published_at = datetime.now(UTC)
                succeeded += 1
                await db.commit()
                continue

            msg.attempt_count += 1
            if msg.attempt_count >= MAX_TRIES:
                # 达上限不再投：把该行折叠归档（摘出 outbox，迁出事件失败表 audit），
                # 不再滞留 pending/failed 挤占领取窗口与积压 gauge（M1 gate review 收口）。
                db.add(
                    EventFailure(
                        event_id=msg.event_id,
                        routing_key=msg.routing_key,
                        payload_json=msg.payload_json,
                        attempt_count=msg.attempt_count,
                        reason="relay exhausted: max tries reached",
                    )
                )
                await db.delete(msg)
            else:
                # 指数退避（cap 1h）保持 pending 待下轮；幂等靠唯一 event_id，不重复入队
                msg.next_retry_at = datetime.now(UTC) + timedelta(
                    seconds=min(2 ** int(msg.attempt_count), _BACKOFF_CAP_S)
                )
            await db.commit()
        if succeeded:
            logger.info("outbox relay 本轮成功 %s 条", succeeded)
        return succeeded
    finally:
        # 积压 gauge：会话仍可分页前统计一遍仍 pending 的件数（含本轮退避、failed 摘除后的剩
        # 余 pending）。统计失败仅记日志（gauge 保上次值），不扰动本应有的 relay 语义。
        try:
            await db.rollback()
            pending_left = await db.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.status == OUTBOX_PENDING)
            )
            outbox_pending_count.set(pending_left or 0)
        except Exception:
            logger.exception("outbox pending gauge 统计失败，保留上次值")
        await db.close()


async def archive_published(
    *,
    retention_s: float | None = None,
    batch: int | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """把**已投递且超过保留期**的行迁到 `outbox_archived` 冷表后从 outbox 删除"""
    retention = (
        settings.outbox_archive_retention_s if retention_s is None else retention_s
    )
    limit = settings.outbox_archive_batch if batch is None else batch
    factory = session_factory or new_session
    db = await factory()
    try:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=retention)
        conds: list[Any] = [
            OutboxMessage.status == OUTBOX_PUBLISHED,
            OutboxMessage.published_at.is_not(None),
            OutboxMessage.published_at < cutoff,
        ]
        # 同 relay_poll：限定 created_at 窗口让 hypertable 做 chunk 裁剪（批 2）。
        window_start = _scan_window_start(now)
        if window_start is not None:
            conds.append(OutboxMessage.created_at >= window_start)
        rows = list(
            (
                await db.execute(
                    select(OutboxMessage)
                    .where(*conds)
                    .order_by(OutboxMessage.id.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0
        archived_at = datetime.now(UTC)
        for msg in rows:
            db.add(
                OutboxArchived(
                    event_id=msg.event_id,
                    routing_key=msg.routing_key,
                    payload_json=msg.payload_json,
                    attempt_count=msg.attempt_count,
                    created_at=msg.created_at,
                    published_at=msg.published_at,
                    archived_at=archived_at,
                )
            )
            await db.delete(msg)
        await db.commit()
        logger.info("outbox 归档 %s 条已发布行（保留期 %ss）", len(rows), retention)
        return len(rows)
    finally:
        await db.close()


# ---- λ leader 租约原语 ----
_redis_degraded_warned = False


def _warn_redis_degraded_once(enabled: bool) -> None:
    """Redis 已配置却拿不到客户端时告警一次（恢复后由 _reset 复位）。"""
    global _redis_degraded_warned
    if enabled and not _redis_degraded_warned:
        _redis_degraded_warned = True
        logger.warning(
            "Redis 已配置但当前不可用：暂停 outbox relay，等待 leader 租约恢复"
        )


def _reset_redis_degraded_warning() -> None:
    global _redis_degraded_warned
    _redis_degraded_warned = False


def _lease_key() -> str:
    env = settings.env or "dev"
    return f"lkm:{env}:outbox:leader"


async def _acquire_lease(redis: Any, ttl_s: float) -> str | None:
    """SET NX EX 抢占租约；成功返回 token，已占用/异常返回 None。"""
    token = uuid.uuid4().hex
    try:
        ok = await redis.set(_lease_key(), token, nx=True, ex=max(1, int(ttl_s)))
    except Exception:
        logger.exception("outbox leader 租约抢占失败，按未取得处理")
        # 抢占异常与争用同记 contended：运维关心的是「本实例没能当选」这一事实
        outbox_leader_total.labels("contended").inc()
        return None
    if ok:
        outbox_leader_total.labels("acquired").inc()
        return token
    outbox_leader_total.labels("contended").inc()
    return None


async def _renew_lease(redis: Any, token: str, ttl_s: float) -> bool:
    """原子续约（值须仍为 token，防误续已让出而双活）；续不上/异常/WatchError→False。"""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for _ in range(3):
                try:
                    await pipe.watch(_lease_key())
                    if await pipe.get(_lease_key()) != token:
                        await pipe.reset()
                        return False  # 已让出/被接管：绝不续别人的租约
                    pipe.multi()
                    pipe.expire(_lease_key(), max(1, int(ttl_s)))
                    await pipe.execute()
                    return True
                except WatchError:
                    await pipe.reset()  # 并发改写竞争，重试乐观锁
    except Exception:
        logger.exception("outbox leader 租约续约异常，按续约失败处理")
    return False


async def _release_lease(redis: Any, token: str) -> None:
    """让出（仅当我们仍持 token 时删除）；异常忽略（TTL 兜底自清）。"""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for _ in range(3):
                try:
                    await pipe.watch(_lease_key())
                    if await pipe.get(_lease_key()) != token:
                        await pipe.reset()
                        return
                    pipe.multi()
                    pipe.delete(_lease_key())
                    await pipe.execute()
                    return
                except WatchError:
                    await pipe.reset()
    except Exception:
        logger.exception("outbox leader 租约释放异常，由 TTL 兜底")


async def run_outbox_loop() -> None:
    """独立进程主循环：周期 poll outbox。"""
    if not settings.message_bus_enabled:
        logger.error("消息总线不可用，outbox relay 空转退出")
        return
    interval = settings.outbox_relay_interval_s
    logger.info(
        "outbox relay 启动（interval=%ss, leader_ttl=%ss）",
        interval,
        settings.outbox_leader_ttl_s,
    )
    token: str | None = None
    # 归档节流：启动即允许首轮（清历史积压），此后按 interval 周期执行；只由当前 poll 者做，与 poll 同循环、不另起任务。
    next_archive_at = datetime.now(UTC)

    async def _poll_tick() -> bool:
        """一轮 poll（到点再归档）；返回 ``False`` = 租约已在轮内失效，调用方需重抢。"""
        nonlocal next_archive_at
        lost = asyncio.Event()

        async def _heartbeat() -> None:
            assert token is not None
            while True:
                await asyncio.sleep(max(0.1, settings.outbox_leader_ttl_s / 3))
                try:
                    renewed = await _renew_lease(
                        redis, token, settings.outbox_leader_ttl_s
                    )
                except Exception:
                    logger.exception("outbox leader heartbeat 异常")
                    renewed = False
                if not renewed:
                    outbox_leader_total.labels("renew_failed").inc()
                    lost.set()
                    return

        heartbeat = asyncio.create_task(_heartbeat()) if token is not None else None
        try:
            try:
                await relay_poll(can_publish=lambda: not lost.is_set())
            except Exception:
                logger.exception("outbox relay_poll 异常，下轮重试")
            if lost.is_set():
                logger.warning("轮内租约续期失败，中止本轮后续投递并重抢")
                return False
            now = datetime.now(UTC)
            if now >= next_archive_at:
                try:
                    await archive_published()
                except Exception:
                    logger.exception("outbox 归档已发布行异常，下个间隔再试")
                next_archive_at = now + timedelta(
                    seconds=settings.outbox_archive_interval_s
                )
            return not lost.is_set()
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    while True:
        try:
            redis = await redis_client.get_redis(_lease_key())
            if redis is None:
                token = None
                # 任一 Redis 后端已配置却拿不到选主客户端，都不能按单实例开发态投递。
                lease_backend_configured = (
                    redis_client.is_enabled() or redis_client.secondary_configured()
                )
                if lease_backend_configured:
                    _warn_redis_degraded_once(True)
                else:
                    # 未配置 Redis 的单实例开发态无需 leader 选举。
                    await _poll_tick()
                await asyncio.sleep(interval)
                continue
            _reset_redis_degraded_warning()

            # 已是 leader → 续约；续不上（被接管/失联）回到未持有。
            if token is not None:
                if not await _renew_lease(redis, token, settings.outbox_leader_ttl_s):
                    outbox_leader_total.labels("renew_failed").inc()
                    logger.info("租约续约失败/已让出，回到外层重抢")
                    token = None
                else:
                    if not await _poll_tick():
                        token = None  # 轮内续约失败：交出 leader 权，下轮重抢
                    await asyncio.sleep(interval)
                    continue

            # 未持有 → 尝试抢占当选。
            token = await _acquire_lease(redis, settings.outbox_leader_ttl_s)
            if token is None:
                logger.info("[follower] 不轮询, leader 由其它副本持有")
                await asyncio.sleep(interval)
                continue
            logger.info("本副本当选 outbox relay leader")
        except asyncio.CancelledError:
            if token is not None:
                r = await redis_client.get_redis(_lease_key())
                if r is not None:
                    await _release_lease(r, token)
            raise
        except Exception:
            logger.exception("outbox relay 外层异常，重新进入领袖 reconcile")
            # 失败路径也要按周期让出：持续性异常（如 Redis 已配置但不可用时反复抛错）若不
            # sleep，会立刻重进 try，形成 CPU 空转 + 每条带完整堆栈的日志风暴
            await asyncio.sleep(interval)
