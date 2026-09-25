"""outbox relay：领取 pending 事件投递到消息总线并置 published（M1.1）。

多副本 leader 选举（M1.2）：`run_outbox_loop` 在 Redis 可用时，以租约键（SET NX EX）维护
「同一时刻仅持租约副本 poll」，其余副本记录 `[follower] 不轮询` 并按周期重试；失联副本
租约 TTL 到期即被接管、事件无缝续投。Redis 未启用（单 owner 开发）时退化为原始单进程
串行 poller，与改动前一致。投递语义=`messaging.publish` 成功即 published。
未配置消息总线 → enqueue 已被 `app/db/outbox.enqueue_outbox` gate 掉不会入队，因此本
poll 也空转退出，与 worker "无 broker 降级空转返回" 一致。

`relay_poll` 刻意收敛为**纯函数**（不启动任何循环/会话生命周期），单测经 monkeypatch /
session_factory seam 注入即可直接驱动；租约判定只出现在 `run_outbox_loop` 运行层。
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import WatchError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import messaging
from app.core import redis as redis_client
from app.core.config import settings
from app.core.metrics import outbox_leader_total, outbox_pending_count
from app.db.event_failure import EventFailure
from app.db.outbox import (
    _BACKOFF_CAP_S,
    MAX_TRIES,
    OUTBOX_PENDING,
    OUTBOX_PUBLISHED,
    OutboxMessage,
)
from app.db.outbox_archive import OutboxArchived
from app.db.session import new_worker_session as new_session

logger = logging.getLogger("lkm.outbox")

# 会话工厂类型：relay_poll 允许单测注入独立内存库会话，默认走生产 async_session(new_session)
SessionFactory = Callable[..., Awaitable[AsyncSession]]

# 本进程标识，写入 `locked_by`（M6.3）：定位「某行被哪个进程认领」，也在排障时区分副本。
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"[:64]


def _scan_window_start(now: datetime) -> datetime | None:
    """扫描窗口的时间下界（批 2，TimescaleDB hypertable 的 chunk 裁剪）。

    ``outbox_scan_window_s <= 0`` 时返回 None（不限窗）。它只用于让规划器跳过
    ``created_at`` 过旧的分区，**不改变「哪些事件可投」的语义**——窗口外的行会被
    Timescale 保留策略 DROP（两者阈值刻意对齐，见 ``init_db._RETENTION_POLICIES``）；
    普通 PG（无 hypertable）上开启它也无副作用，只是少扫陈年滞留行。
    """
    if settings.outbox_scan_window_s <= 0:
        return None
    return now - timedelta(seconds=settings.outbox_scan_window_s)


def _claimable(now: datetime) -> tuple[Any, ...]:
    """可领取窗口的 WHERE 条件：到期待投 **且** 未被别的进程有效认领（M6.3）。

    锁列（`locked_at/locked_by`）此前只建不用；现在领取即写、投递后清，并叠加陈旧阈值：
    `locked_at` 比 TTL 更早的行视为「持有者已崩溃」，可被重新领取——否则持锁进程崩溃会让
    该行永久卡死。未到期（`locked_at` 新鲜）的行留给持有者，别的副本不抢。

    另叠加 ``created_at`` 时间窗（批 2，见 :func:`_scan_window_start`）。
    """
    stale_before = now - timedelta(seconds=settings.outbox_lock_ttl_s)
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
    batch: int = 100, *, session_factory: SessionFactory | None = None
) -> int:
    """扫一批到期的 pending 事件投递；返回本轮成功(published)事件数。

    语义：
    - 领取窗口 = `status=pending AND next_retry_at<=now` **且未被有效认领**
      （`locked_at IS NULL OR locked_at < now-lock_ttl`，见 :func:`_claimable`），
      按 attempt 升序（少重试者在先），`FOR UPDATE SKIP LOCKED` 避免与并发 poller 阻塞互等。
    - 领取即写 `locked_at/locked_by` 并 commit（释放行级锁、留下跨事务的认领标记），
      投递尝试结束（成功或失败）即 :func:`_clear_lock`——标记只用于「同刻不被两个副本各取走」，
      不改变退避语义；持标记进程崩溃 → 超 `outbox_lock_ttl_s` 后该行可被重新领取。
    - 投递（`messaging.publish(routing_key, parsed)`）成功 → `published_at=now, status=published`。
    - **永久失败分类**（M6.3）：投递前经 `messaging.permanent_failure_reason` 判定
      （未知 routing_key / payload 不可编码）→ **不消耗重试额度**，一次即折叠进 `event_failures`；
      其余失败视为瞬时（总线不可达等）→ `attempt_count += 1`，达 `MAX_TRIES` 折叠归档，
      否则指数退避 `next_retry_at = now + 2**attempt s`（cap 1h）保持 pending 待下轮。
    - 每事件独立 flush/commit，单条失败不影响其余。
    - 多副本注（M1 gate review 收钝）：同刻唯一 poll 仍由 leader 租约(M1.2)保证；本层的
      SKIP LOCKED + 认领标记是 leader 内多线程/接管窗口的兜底。最外正确性仍靠消费端
      event_id 幂等 + handler 硬次级幂等(points ref 唯一 / notify GETDEL)。
    - 可观测（M0.5.2）：每轮末尾统计表内仍 `status=pending`（含退避等待下一轮）件数
      set 到 `outbox_pending_count` gauge 供积压看板。投递失败计数不在此重复——提交经
      `messaging.publish`，其抛出/不可用路径已由 messaging 层自身计 `notify_failed_total`。
    """
    factory = session_factory or new_session
    db = await factory()
    succeeded = 0
    try:
        now = datetime.now(UTC)
        rows = list(
            (
                await db.execute(
                    select(OutboxMessage)
                    .where(*_claimable(now))
                    .order_by(OutboxMessage.attempt_count.asc(), OutboxMessage.id.asc())
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if rows:
            # 领取到的行里若有 locked_at 非空者，说明持有者已崩溃、本条是被超期接管的陈旧锁
            # （见 _claimable 的 stale_before 条件）。蓝图 §5.1 第 7 条要求这类接管可观测。
            stale = sum(1 for m in rows if m.locked_at is not None)
            if stale:
                outbox_leader_total.labels("stale_reclaimed").inc(stale)
            # 认领落库（先 commit 释放行锁）：后续其它 poller 会跳过这些行直到清标记/超时。
            claimed_at = datetime.now(UTC)
            for msg in rows:
                msg.locked_at = claimed_at
                msg.locked_by = _INSTANCE_ID
            await db.commit()

        for msg in rows:
            # 把 outbox 幂等键 event_id 透传进发布消息，消费端据此按「已处理记账」去重
            # （M1.3；见 app/db/event_processed.py）。fn/args 原样保留，多余键对 handler 无害。
            payload = {**msg.payload_json, "event_id": msg.event_id}
            reason = messaging.permanent_failure_reason(msg.routing_key, payload)
            if reason is not None:
                # 确定性错误：重试不会变好，一次即折叠（不累加 attempt_count，不空耗退避）。
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

            try:
                ok = await messaging.publish(msg.routing_key, payload)
            except Exception:
                logger.exception(
                    "outbox publish exception id=%s rk=%s", msg.id, msg.routing_key
                )
                ok = False

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
        if rows and succeeded:
            logger.info("outbox relay 本轮成功 %s 条", succeeded)
        return succeeded
    finally:
        # 积压 gauge：会话仍可分页前统计一遍仍 pending 的件数（含本轮退避、failed 摘除后的剩
        # 余 pending）。统计失败仅记日志（gauge 保上次值），不扰动本应有的 relay 语义。
        try:
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
    """把**已投递且超过保留期**的行迁到 `outbox_archived` 冷表后从 outbox 删除（M6.3）。

    纪律：**先归档后删**（同一事务内先 insert 冷副本再 delete 原行）——删除是不可逆操作，
    蓝图为它定的前提是「先留冷副本」，故本函数是唯一允许删已发布行的入口。

    - 只动 `status=published AND published_at < now-retention` 的行；pending/failed 一律不碰
      （它们仍在生命周期中：pending 待投、failed 理论上不会留在 outbox——达上限即折叠）。
    - `SKIP LOCKED` + 批上限：与服务化 relay 并发时不会互锁，单轮工作量有界。
    - 返回本轮归档（=删除）条数；无候选返回 0。
    """
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
        logger.info(
            "outbox 归档 %s 条已发布行（保留期 %ss）", len(rows), retention
        )
        return len(rows)
    finally:
        await db.close()


# ---- λ leader 租约原语（M1.2，仿 cache.py make_key / _PING_TIMEOUT fail-open）----
# 租约键采用 cache.py 的命名规范 `lkm:{env}:outbox:leader`，与其它 Redis 键共用 env 隔离。
# 原语对命令异常一律 fail-open 返回「未取得/未续成」，由 run_outbox_loop 据此保守地不轮询
# （宁可短暂积压也不多副本重复投），与限流器 fail 语义分场景定性一致。


# 「Redis 已配置但不可用」的告警去重标记：get_redis() 的 fail-open 让它与「未配置」都
# 表现为 None，此分支每 interval 走一次，不去重会把日志刷满
_redis_degraded_warned = False


def _warn_redis_degraded_once(enabled: bool) -> None:
    """Redis 已配置却拿不到客户端时告警一次（恢复后由 _reset 复位）。"""
    global _redis_degraded_warned
    if enabled and not _redis_degraded_warned:
        _redis_degraded_warned = True
        logger.warning(
            "Redis 已配置但当前不可用：outbox relay 退化为无租约串行 poll"
            "（多副本部署下各副本会并发轮询，仅靠 SKIP LOCKED 与消费端幂等兜底）"
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
        ok = await redis.set(_lease_key(), token, nx=True, ex=int(ttl_s))
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
                    pipe.expire(_lease_key(), int(ttl_s))
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
    """独立进程主循环：周期 poll outbox（未配置消息总线空转退出，语义同 worker）。

    - 未配置消息总线：空转退出（enqueue 已被 gate，无事件可 poll）。
    - Redis 未启用/不可用（单 owner 开发）：直接串行 poll，等同改动前 M1.1 行为。
    - Redis 可用：以租约维持 leader 权。每个 tick 先 reconcile：仍是 leader 则续约 poll；
      已让出/未持有则尝试 NX 抢占——占不到说明被别的副本持有，记 `[follower] 不轮询`
      并按 interval 重试，直到原 leader 失联 TTL 到期被接管为新 leader。失联接管延迟
      上界 ≈`outbox_leader_ttl_s`。ttl(60s) 远大于 interval(2s)，故每 tick 续一次足额，
      不会抖动抢主。
    """
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
    # 归档节流（M6.3）：启动即允许首轮（清历史积压），此后按 interval 周期执行；只由
    # 当前 poll 者做（单 owner 或 leader），与 poll 同循环、不另起任务。
    next_archive_at = datetime.now(UTC)

    async def _poll_tick() -> bool:
        """一轮 poll（到点再归档）；返回 ``False`` = 租约已在轮内失效，调用方需重抢。"""
        nonlocal next_archive_at
        try:
            await relay_poll()
        except Exception:
            logger.exception("outbox relay_poll 异常，下轮重试")
        # 轮内续约：单轮 relay_poll(batch=100) + archive_published(batch=500) 在总线变慢/
        # 积压大时可能超过 outbox_leader_ttl_s，而租约只在 tick 进入前续过一次——不续期
        # 就会「本副本仍在投递、租约已到期」，被其它副本抢成双 leader 并发 poll/归档。
        # token 为 None（Redis 不可用的单 owner 路径）时无租约可续，直接跳过。
        if token is not None and not await _renew_lease(
            redis, token, settings.outbox_leader_ttl_s
        ):
            outbox_leader_total.labels("renew_failed").inc()
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

    while True:
        try:
            redis = await redis_client.get_redis()
            if redis is None:
                # 单 owner 开发态（未配 Redis）：无副本竞争，直接串行 poll，等同 M1.1。
                # 「已配置但暂时不可用」走同一分支：这里刻意继续投递（fail-open 保可用性），
                # 同时告警一次——若改成直接跳过，Redis 一挂 outbox 投递就整体停摆；而 poll
                # 走 SKIP LOCKED，多副本并发只会各取不相交的一批行。丢租约的代价仅是多副本
                # 下并发轮询（部署清单 replicas=1，且消费者按 event_id 幂等）。
                token = None
                _warn_redis_degraded_once(redis_client.is_enabled())
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
                r = await redis_client.get_redis()
                if r is not None:
                    await _release_lease(r, token)
            raise
        except Exception:
            logger.exception("outbox relay 外层异常，重新进入领袖 reconcile")
            # 失败路径也要按周期让出：持续性异常（如 Redis 已配置但不可用时反复抛错）若不
            # sleep，会立刻重进 try，形成 CPU 空转 + 每条带完整堆栈的日志风暴
            await asyncio.sleep(interval)
