"""跨进程业务指标中继：非 API 进程经 Redis 由 API 进程代报。
Redis 不可用两侧都 fail-open：写失败只记日志；读失败保留上次值并把 ``metrics_relay_up`` 置 0
（"不知道远端状态"与"远端异常"对告警同解——宁可吵，不可沉默）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Literal

from core import redis as redis_client
from core.cache import make_key
from core.config import settings
from core.metrics import (
    audit_events_total,
    cache_lock_total,
    content_index_events_total,
    counts_reconcile_repeated_total,
    event_contract_violations_total,
    metrics_relay_instances,
    metrics_relay_up,
    notify_failed_total,
    outbox_leader_total,
    outbox_pending_count,
    user_snap_cache_total,
    user_snap_singleflight_total,
)

logger = logging.getLogger("lkm.metrics_relay")

Kind = Literal["gauge", "counter"]


@dataclass(frozen=True)
class RelaySpec:
    """一个需要跨进程中继的指标：导出名 + 类型 + label 名 + 指标对象单例。"""

    name: str
    kind: Kind
    labelnames: tuple[str, ...]
    metric: Any


# 中继清单 = 唯一事实源
RELAYED: tuple[RelaySpec, ...] = (
    # worker-outbox（app/core/outbox_relay.py）：积压 gauge + 选主事件计数，lkm-outbox.yml 靠它判活
    RelaySpec("outbox_pending_count", "gauge", (), outbox_pending_count),
    RelaySpec("outbox_leader_total", "counter", ("event",), outbox_leader_total),
    # messaging.publish 的失败计数：worker-outbox/scheduler/dlq 与 API、auth 都有调用点
    RelaySpec("notify_failed_total", "counter", (), notify_failed_total),
    # user:snap 读模型命中率/合并收益：API + notification worker + auth 三处都写
    RelaySpec(
        "user_snap_cache_total", "counter", ("layer", "result"), user_snap_cache_total
    ),
    RelaySpec(
        "user_snap_singleflight_total",
        "counter",
        ("role",),
        user_snap_singleflight_total,
    ),
    # cached_read 的跨进程锁：现仅 API 写，纳入以兜住「worker 也开始用缓存的读写路径」的变化
    RelaySpec("cache_lock_total", "counter", ("result",), cache_lock_total),
    # content.* 增量同步（仅 content-index worker）
    RelaySpec(
        "content_index_events_total",
        "counter",
        ("action",),
        content_index_events_total,
    ),
    # 计数对账震荡（仅 jobs worker 的 cron）
    RelaySpec(
        "counts_reconcile_repeated_total",
        "counter",
        (),
        counts_reconcile_repeated_total,
    ),
    # audit.* 消费计数（handler 折在 worker_default 里）
    RelaySpec("audit_events_total", "counter", ("action",), audit_events_total),
    # 事件契约违约：consume 侧写出点在 worker 进程（worker._on_payload），produce 侧在
    # relay/scheduler/API/auth 都有——两侧都要能在 API 进程的 /metrics 上看见
    RelaySpec(
        "event_contract_violations_total",
        "counter",
        ("fn", "side"),
        event_contract_violations_total,
    ),
)

_SPEC_BY_NAME = {spec.name: spec for spec in RELAYED}

# 本实例标识：写进快照键，多副本互不覆盖；hostname+pid 在容器内唯一。
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"[:128]

# TTL 取周期的 3 倍：允许漏两拍（写失败/短暂不可用）而不误判"生产者在"，同时又能在一个合理
# 窗口内发现"进程真的没了"。理由与取值同 scheduler_state。
_TTL_MULTIPLIER = 3


def relay_key(instance: str | None = None) -> str:
    """本实例（或指定实例）的快照键；带 env 命名空间，避免 dev/prod 共用一个 Redis 互相污染。"""
    return make_key("metrics", "relay", instance or _INSTANCE_ID)


def relay_key_pattern() -> str:
    """扫描全部存活实例快照的 match 模式。"""
    return make_key("metrics", "relay", "*")


# ---- 生产者侧（worker / scheduler / auth）----


def snapshot() -> list[list[Any]]:
    """
    读本进程 ``RELAYED`` 各指标的当前值：``[[name, [label_value...], value], ...]``。
    """
    out: list[list[Any]] = []
    for spec in RELAYED:
        try:
            for metric in spec.metric.collect():
                for sample in metric.samples:
                    if sample.name != spec.name:
                        continue
                    out.append(
                        [
                            spec.name,
                            [sample.labels[k] for k in spec.labelnames],
                            sample.value,
                        ]
                    )
        except Exception:
            # 单个指标取值失败不能让整份快照丢失——其余指标照常上报
            logger.warning("指标取值失败 name=%s", spec.name, exc_info=True)
    return out


async def publish_snapshot(redis: Any | None = None) -> bool:
    """把本进程快照写进 Redis（带 TTL）；返回是否写成功。Redis 不可用 → False（fail-open）。"""
    client = redis if redis is not None else await redis_client.get_redis(relay_key())
    if client is None:
        return False
    ttl = max(1, int(settings.metrics_relay_interval_s * _TTL_MULTIPLIER))
    try:
        await client.set(
            relay_key(),
            json.dumps(snapshot(), separators=(",", ":")),
            ex=ttl,
        )
        return True
    except Exception:
        logger.warning("指标快照写入失败（fail-open）", exc_info=True)
        return False


async def run_publisher(interval_s: float | None = None) -> None:
    """生产者心跳循环（由调用方 cancel 收尾，与 ``scheduler_state.run_heartbeat`` 同款）。"""
    period = settings.metrics_relay_interval_s if interval_s is None else interval_s
    period = max(period, 1.0)  # 下界 1s：配成 0/负数会退化成紧凑轮询
    while True:
        await publish_snapshot()
        await asyncio.sleep(period)


_publisher: asyncio.Task[None] | None = None


def start_publisher() -> None:
    """
    在非 API 进程启动指标快照发布（幂等；未启用指标则整体不启动）。
    """
    global _publisher
    if not settings.metrics_enabled:
        return
    if _publisher is None or _publisher.done():
        _publisher = asyncio.create_task(run_publisher())
        logger.info("指标中继发布已启动 instance=%s", _INSTANCE_ID)


async def stop_publisher() -> None:
    """取消发布任务（幂等/可重复调用）。"""
    global _publisher
    task, _publisher = _publisher, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("指标中继发布任务此前已异常退出", exc_info=True)


# ---- 消费者侧（仅 API 进程）----

# (指标名, label 值元组) → 上一次读到的远端合计。只在本进程内有意义（进程重启即清空，
# 与本地 Counter 同时归零，二者一致）。
_remote_totals: dict[tuple[str, tuple[str, ...]], float] = {}


def _reset_remote_totals() -> None:
    """清空远端基线（测试用；也供将来"指标被重置"的显式场景）。"""
    _remote_totals.clear()


def _parse_payload(
    raw: Any, key: str = ""
) -> list[tuple[str, tuple[str, ...], float]] | None:
    """
    把一份快照载荷解析成 ``[(name, labels, value)]``；载荷整体非法返回 None。
    """
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, list):
        return None
    entries: list[tuple[str, tuple[str, ...], float]] = []
    for item in decoded:
        if not isinstance(item, list) or len(item) != 3:
            logger.debug("指标快照条目形状非法，忽略 key=%s item=%r", key, item)
            continue
        name, labels, value = item
        spec = _SPEC_BY_NAME.get(name) if isinstance(name, str) else None
        if spec is None or not isinstance(labels, list):
            logger.debug("指标快照条目名未知，忽略 key=%s item=%r", key, item)
            continue
        if len(labels) != len(spec.labelnames):
            logger.debug(
                "指标快照 label 个数与清单不符，忽略 key=%s item=%r", key, item
            )
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            logger.debug("指标快照条目值非数字，忽略 key=%s item=%r", key, item)
            continue
        entries.append((name, tuple(str(v) for v in labels), float(value)))
    return entries


def _apply_counter(name: str, labels: tuple[str, ...], total: float) -> None:
    """把远端合计的**增量**落到本地 Counter（远端回退只重置基线，见模块 docstring）。"""
    spec = _SPEC_BY_NAME[name]
    key = (name, labels)
    last = _remote_totals.get(key, 0.0)
    delta = total - last
    if delta <= 0:
        _remote_totals[key] = total
        return
    try:
        child = spec.metric if not spec.labelnames else spec.metric.labels(*labels)
        child.inc(delta)
    except Exception:
        # 施加失败不更新基线，下一轮重试（否则这段增量被永久吞掉）
        logger.warning(
            "远端计数施加失败 name=%s labels=%s", name, labels, exc_info=True
        )
        return
    _remote_totals[key] = total


def _apply_gauge(name: str, labels: tuple[str, ...], value: float) -> None:
    spec = _SPEC_BY_NAME[name]
    try:
        child = spec.metric if not spec.labelnames else spec.metric.labels(*labels)
        child.set(value)
    except Exception:
        logger.warning(
            "远端 gauge 施加失败 name=%s labels=%s", name, labels, exc_info=True
        )


async def collect_once(redis: Any | None = None) -> None:
    """扫一遍全部实例快照并落到本地指标（API 进程调用）。"""
    # SCAN 的 pattern 落在 metrics:relay 前缀内 → 该前缀整体属于同一后端
    client = (
        redis
        if redis is not None
        else await redis_client.get_redis(relay_key_pattern())
    )
    if client is None:
        # 读不到远端状态：按"不可认为中继在跑"处置，但不改动任何业务指标的上次值
        metrics_relay_up.set(0)
        return

    counter_totals: dict[tuple[str, tuple[str, ...]], float] = {}
    gauge_values: dict[tuple[str, tuple[str, ...]], float] = {}
    instances = 0
    try:
        async for key in client.scan_iter(match=relay_key_pattern()):
            try:
                raw = await client.get(key)
            except Exception:
                # 单键读失败不中断其余实例（保留它上一轮的贡献基线）
                logger.warning("指标快照读取失败 key=%s", key, exc_info=True)
                continue
            if not raw:
                continue
            entries = _parse_payload(raw, key)
            if entries is None:
                logger.warning("指标快照载荷损坏，按不存在处理 key=%s", key)
                continue
            instances += 1
            for name, labels, value in entries:
                key2 = (name, labels)
                if _SPEC_BY_NAME[name].kind == "counter":
                    counter_totals[key2] = counter_totals.get(key2, 0.0) + value
                else:
                    gauge_values[key2] = max(gauge_values.get(key2, value), value)
    except Exception:
        logger.warning("指标快照扫描失败（置 up=0）", exc_info=True)
        metrics_relay_up.set(0)
        return

    for (name, labels), total in counter_totals.items():
        _apply_counter(name, labels, total)
    for (name, labels), value in gauge_values.items():
        _apply_gauge(name, labels, value)

    metrics_relay_instances.set(float(instances))
    metrics_relay_up.set(1 if instances else 0)


_task: asyncio.Task[None] | None = None


async def _run_reporter() -> None:
    # 下界 1s，理由同 run_publisher
    period = max(settings.metrics_relay_interval_s, 1.0)
    while True:
        try:
            await collect_once()
        except Exception:
            # 轮级兜底：任一轮异常不得让 reporter 永久停更（否则指标静默冻结在旧值）
            logger.exception("指标中继上报轮次异常，跳过本轮")
        await asyncio.sleep(period)


def start_reporter() -> None:
    """在 API 进程启动中继上报（幂等；指标关闭 / Redis 未配置则整体空转）。"""
    global _task
    if not settings.metrics_enabled:
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_run_reporter())
        logger.info("跨进程指标中继上报已启动")


async def stop_reporter() -> None:
    """取消上报任务（幂等/可重复调用）。"""
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("指标中继上报任务此前已异常退出", exc_info=True)
