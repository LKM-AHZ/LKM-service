"""
每用户只读快照缓存 `user:snap:{id}`：cache-through + 版本 CAS + 反陈旧防复活血统。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from redis import WatchError
from redis.asyncio import Redis as _AsyncRedis

import core.bloom as bloom
import core.local_cache as local_cache
import core.redis as redis_client
import core.user_cache_events as user_cache_events
from core.cache import TTL_ITEM_S, jitter_ttl, make_key
from core.config import settings
from core.metrics import user_snap_cache_total

logger = logging.getLogger("lkm.user_cache")

# 失效代次键不存在时视作 epoch 0。
_EPOCH_ABSENT = 0
# WATCH 冲突达到重试上限后停止回填。
_MAX_CAS_RETRY = 8

_NEG_SV = 0
_NEG_TTL_S = 30  # 短 TTL：真实用户随后被创建时，最多 30s 后即可见（远短于正常快照 TTL）
_L1_TTL_LOWER_ONLY = True  # L1 的 TTL 是「丢广播时的陈旧窗口上界」，只可向下扰动（见 jitter_ttl）


def _snap_key(user_id: uuid.UUID) -> str:
    return make_key("user:snap", user_id)


def _epoch_key(user_id: uuid.UUID) -> str:
    return make_key("user:snap:ver", user_id)


def get_user_cache_key(user_id: uuid.UUID) -> str:
    """单用户快照的缓存键（导出，测试/观测断言用）。"""
    return _snap_key(user_id)


def _l1_on() -> bool:
    """L1 是否启用：配置开关且 Redis 已配置（Redis 关闭则 L1 一并关闭，避免无法跨实例失效）。"""
    return settings.user_snap_l1_enabled and redis_client.is_enabled()


def version_of_updated_at(updated_at: datetime) -> int:
    """
    User.updated_at → 单调可比的来源版本 int（秒*1e6 + 微秒，归一 UTC 后取绝对刻度）。
    - 输入带/不带 tz 均先规整到 UTC 再取 ``timestamp*1e6 + tz-mic``，跨进程确定性可比、且随时刻单调不减。
    - 微秒为 compare 粒度：同微秒内多次变更同版本是已知边界局限（随 A7 双失效补齐，见 docstring）。
    """
    dt = updated_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000 + utc.microsecond


async def _get_redis(key: str | None = None) -> _AsyncRedis | None:
    """取 Redis 客户端；``key`` 用于按前缀路由到对应后端（见 core.redis）。"""
    return await redis_client.get_redis(key)


def _normalize_snap(data: dict[str, Any]) -> dict[str, Any]:
    """
    把快照 dict 内的 ``user_id`` 归一为 ``uuid.UUID``。
    Redis 里 uuid 以 JSON 字符串存放（写侧 :func:`write_if_newer` 用 ``default=str``），而
    DB 直读路径给出的是 ``uuid.UUID``；不归一会让「缓存命中」与「miss 回填」重建出的
    ``UserSnapshot.user_id`` 类型不一致（str vs UUID），下游按 id 比较随即失真。
    已是 UUID或本无该字段则原样返回。
    """
    uid = data.get("user_id")
    if isinstance(uid, str):
        try:
            return {**data, "user_id": uuid.UUID(uid)}
        except ValueError:
            return data
    return data


async def current_epoch(user_id: uuid.UUID) -> int:
    """
    读当前失效代次（快照读缝 DB 回填前调用、作为写时 expected_epoch）。fail-open→0。
    首次访问会**初始化一个非 0 且不重复的起始代次**（微秒时间戳，``SET NX`` 并发安全）。
    这是关掉 Redis 持久化后的关键一环：epoch 键重启即丢，而「键从未存在」与「键被重启抹掉」
    都会表现为读不到——若两者都记作 0，一个在重启前捕获 ``expected_epoch=0`` 的在途回填，
    在重启后会看到当前值仍是 0，被误判为「期间未失效」而把**陈旧快照写回**（:func:`write_if_newer`
    的两道守卫都失效）。改用不重复的起始值后，「重启前捕获」与「重启后重读」必然对不上 →
    陈旧回填被拒，缓存保持空、下个读拉回 DB 实况。
    """
    redis = await _get_redis(_epoch_key(user_id))
    if redis is None:
        return _EPOCH_ABSENT
    ekey = _epoch_key(user_id)
    try:
        raw = await redis.get(ekey)
        if raw is None:
            # 初始化写入非零代次，并发调用共用已写入的值。
            await redis.set(ekey, str(time.time_ns() // 1000), nx=True)
            raw = await redis.get(ekey)
    except Exception:
        return _EPOCH_ABSENT
    return _EPOCH_ABSENT if raw is None else _to_int(raw)


def _to_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _EPOCH_ABSENT


async def read_snap_state(
    user_id: uuid.UUID,
) -> tuple[bool, dict[str, Any] | None]:
    """
    读快照并区分「未缓存」与「已确认不存在（负值缓存）」，返回 ``(negative, data)``。
    ``negative=True`` 表示上游权威已判定该用户不存在、且仍在负值窗口内——调用方应**直接按
    不存在处理**，不必再回退上游（这正是防穿透的收益）。
    L1 命中直接返回（免 L2 往返）；L1 miss 才查 L2，L2 命中后按 L1 TTL 回填本地。L1 条目
    仅作镜像，脏形态（非 dict）即删，不放大既有 ``_from_cache_dict`` 的脏缓存问题。
    **负值不进 L1**（L1 是正向值镜像，短 TTL 的负值不必占本地内存）。
    L2 读取期间若本进程收到失效广播或另一协程已回填目标键，放弃本次 L1 回填。
    pub/sub 本身不保证送达，丢广播仍由 L1 短 TTL（默认 10s）兜底。
    """
    redis = await _get_redis(_snap_key(user_id))
    if redis is None:
        return False, None
    key = _snap_key(user_id)
    if _l1_on():
        entry = local_cache.l1_get(key)
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict):
                user_snap_cache_total.labels("l1", "hit").inc()
                return False, _normalize_snap(data)
            local_cache.l1_delete(key)
        user_snap_cache_total.labels("l1", "miss").inc()
    revision = local_cache.l1_invalidation_revision()
    try:
        raw = await redis.get(key)
    except Exception:
        logger.debug("user_cache get fail-open uid=%s", user_id)
        return False, None
    if raw is None:
        user_snap_cache_total.labels("l2", "miss").inc()
        if await bloom.definitely_absent(str(user_id)):
            logger.debug("user_cache bloom-reject uid=%s", user_id)
            return True, None
        logger.debug("user_cache miss uid=%s", user_id)
        return False, None
    user_snap_cache_total.labels("l2", "hit").inc()
    try:
        payload = json.loads(raw)
        if payload.get("neg"):
            # 负值缓存命中后直接返回不存在。
            return True, None
        data = payload.get("data")
        if not isinstance(data, dict):
            return False, None
        data = _normalize_snap(data)
        if _l1_on():
            sv = payload.get("sv")
            local_cache.l1_set_if_unchanged(
                key,
                {"sv": _to_int(sv) if sv is not None else None, "data": data},
                jitter_ttl(settings.user_snap_l1_ttl_s, lower_only=_L1_TTL_LOWER_ONLY),
                revision,
            )
        return False, data
    except Exception:
        # 非法 L2 载荷记日志并按未命中处理。
        logger.debug("user_cache payload 解析失败 uid=%s", user_id, exc_info=True)
        return False, None


async def read_snap(user_id: uuid.UUID) -> dict[str, Any] | None:
    """
    读快照数据 dict（L1 本地 → L2 Redis）；未命中/Redis 故障 → None（miss 由 DB 兜底）。
    负值缓存命中同样返回 None——需区分「已确认不存在」与「未缓存」的调用方用
    :func:`read_snap_state`（本函数是它的薄包装，保留既有调用方契约）。
    """
    _, data = await read_snap_state(user_id)
    return data


async def read_snaps_state(
    user_ids: list[uuid.UUID],
) -> tuple[set[uuid.UUID], dict[uuid.UUID, dict[str, Any]]]:
    """
    批量读快照，返回 ``(negative_ids, data_map)``。
    ``negative_ids`` = 命中负值缓存（上游已确认不存在，窗口内**不必回退上游**）的 id 集合；
    ``data_map`` = 命中的字段 dict。**两者都不含**的 id 才是真 miss（需回退上游）。
    语义与逐 id 调 :func:`read_snap_state` 等价（同一套 L1/L2 与命中指标），差别只在把 N 次
    L2 往返收成 1 次——M6.5 批量读（by-ids）的收益正是在此。Redis 不可用/异常 fail-open → 空
    （调用方走上游拉取），绝不抛错。
    """
    if not user_ids:
        return set(), {}
    # 批量快照键按首键选择缓存后端。
    redis = await _get_redis(_snap_key(user_ids[0]))
    if redis is None:
        return set(), {}
    l1 = _l1_on()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    negative: set[uuid.UUID] = set()
    pending: list[uuid.UUID] = []
    entries = local_cache.l1_multi_get([_snap_key(uid) for uid in user_ids]) if l1 else []
    for index, uid in enumerate(user_ids):
        if l1:
            entry = entries[index]
            if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
                user_snap_cache_total.labels("l1", "hit").inc()
                out[uid] = _normalize_snap(entry["data"])
                continue
            if entry is not None:
                local_cache.l1_delete(_snap_key(uid))  # 脏形态即删，不放大问题
            user_snap_cache_total.labels("l1", "miss").inc()
        pending.append(uid)
    revision = local_cache.l1_invalidation_revision()
    if pending:
        # 布隆过滤器确认不存在的 ID 直接并入 negative。
        absent = await bloom.definitely_absent_many([str(uid) for uid in pending])
        if absent:
            negative.update(uid for uid in pending if str(uid) in absent)
            pending = [uid for uid in pending if str(uid) not in absent]
    if not pending:
        return negative, out
    try:
        raws = await redis.mget([_snap_key(uid) for uid in pending])
    except Exception:
        logger.debug("user_cache mget fail-open n=%s", len(pending))
        return negative, out
    for uid, raw in zip(pending, raws, strict=True):
        if raw is None:
            user_snap_cache_total.labels("l2", "miss").inc()
            continue
        user_snap_cache_total.labels("l2", "hit").inc()
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if payload.get("neg"):
            negative.add(uid)  # 负值命中：窗口内不再回退上游
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        data = _normalize_snap(data)
        out[uid] = data
        if l1:
            sv = payload.get("sv")
            local_cache.l1_set_if_unchanged(
                _snap_key(uid),
                {"sv": _to_int(sv) if sv is not None else None, "data": data},
                jitter_ttl(settings.user_snap_l1_ttl_s, lower_only=_L1_TTL_LOWER_ONLY),
                revision,
            )
    return negative, out


async def read_snaps(user_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """
    批量读快照；未命中的 id（含负值命中）不在结果里。
    需区分「上游已确认不存在」的调用方用 :func:`read_snaps_state`（本函数是其薄包装，
    保留既有调用方契约）。
    """
    _, data_map = await read_snaps_state(user_ids)
    return data_map


async def read_snap_with_version(
    user_id: uuid.UUID,
) -> tuple[int | None, dict[str, Any] | None]:
    """
    读缓存返回 `(sv, data)`（测试断言存内源版本用）；未命中/故障返回 `(None, None)`。
    同 :func:`read_snap` 走 L1 → L2，L2 命中回填 L1（信封含 sv，供版本断言）。
    """
    redis = await _get_redis(_snap_key(user_id))
    if redis is None:
        return None, None
    key = _snap_key(user_id)
    if _l1_on():
        entry = local_cache.l1_get(key)
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict):
                sv = entry.get("sv")
                return (int(sv) if sv is not None else None), _normalize_snap(data)
            local_cache.l1_delete(key)
    revision = local_cache.l1_invalidation_revision()
    try:
        raw = await redis.get(key)
    except Exception:
        return None, None
    if raw is None:
        return None, None
    try:
        p = json.loads(raw)
        sv = _to_int(p.get("sv")) if p.get("sv") is not None else None
        data = p.get("data")
        if isinstance(data, dict):
            data = _normalize_snap(data)
        if _l1_on() and isinstance(data, dict):
            local_cache.l1_set_if_unchanged(
                key,
                {"sv": sv, "data": data},
                jitter_ttl(settings.user_snap_l1_ttl_s, lower_only=_L1_TTL_LOWER_ONLY),
                revision,
            )
        return sv, data
    except Exception:
        return None, None


async def write_if_newer(
    user_id: uuid.UUID,
    data: dict[str, Any] | None,
    source_version: int,
    expected_epoch: int,
    *,
    ttl_seconds: int = TTL_ITEM_S,
    negative: bool = False,
) -> bool:
    """
    CAS 回填：sv 胜过已存值**且** epoch 未被失效 bump 才写入；否则拒写返回 False。
    原子实现 = WATCH[snap, epoch] + MULTI 乐观锁（repo M1.2 同款，fakeredis 可跑、生产零外部
    脚本依赖；不用 Lua/eval——fakeredis 不支持 eval/lupa）。陈旧/已失效拒写是**确定性**结果，
    直接返回不重试；只有 WatchError 代表的真并发竞态才乐观重试。
    ``negative=True``（配 ``data=None``）写的是**负值缓存**（上游确认不存在，见
    :func:`write_negative`）：用短 ``ttl_seconds``、且**不进 L1**（L1 只镜像正向值）。
    TTL 一律经 :func:`jitter_ttl` 扰动防雪崩（蓝图 §5.6）。
    """
    key = _snap_key(user_id)
    ekey = _epoch_key(user_id)
    redis = await _get_redis(key)
    if redis is None:
        return False
    payload: dict[str, Any] = {"sv": source_version, "data": data}
    if negative:
        payload["neg"] = True
    value = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for _ in range(_MAX_CAS_RETRY):
                try:
                    await pipe.watch(key, ekey)
                    cur_epoch = _to_int(await pipe.get(ekey))
                    raw_cur_snap = await pipe.get(key)
                    # 仅写入不旧于缓存的来源版本。
                    if raw_cur_snap is not None:
                        cur_sv = _extract_sv(raw_cur_snap)
                        if cur_sv is not None and cur_sv > source_version:
                            await pipe.reset()
                            return False
                    # 失效代次变化时拒绝回填。
                    if cur_epoch != expected_epoch:
                        await pipe.reset()
                        return False
                    pipe.multi()
                    pipe.set(key, value, ex=jitter_ttl(ttl_seconds))
                    revision = local_cache.l1_invalidation_revision()
                    await pipe.execute()
                    if (
                        _l1_on()
                        and not negative
                        and revision == local_cache.l1_invalidation_revision()
                    ):
                        existing = local_cache.l1_get(key)
                        if not (
                            isinstance(existing, dict)
                            and _to_int(existing.get("sv")) > source_version
                        ):
                            local_cache.l1_set(
                                key,
                                {"sv": source_version, "data": data},
                                jitter_ttl(
                                    settings.user_snap_l1_ttl_s,
                                    lower_only=_L1_TTL_LOWER_ONLY,
                                ),
                            )
                    return True
                except WatchError:
                    await pipe.reset()
    except Exception:
        logger.exception("user_cache write CAS 异常，按未写入处理 uid=%s", user_id)
    return False


async def write_negative(user_id: uuid.UUID, expected_epoch: int) -> bool:
    """
    把「上游权威确认不存在」记为负值缓存（短 TTL），防不存在 id 反复穿透上游（§5.6）。
    与 :func:`write_if_newer` 同一套 CAS/epoch 守卫：负值 sv=0，任何真实正 sv 的回填都能
    覆盖它（版本条件只拒「更旧」，0 不拒任何正 sv）；被失效 bump 后，在途的负写也会被代次
    条件拒掉，不会把已删用户「复活」成不存在。写失败静默返回 False，不影响读语义。
    """
    return await write_if_newer(
        user_id,
        None,
        _NEG_SV,
        expected_epoch,
        ttl_seconds=_NEG_TTL_S,
        negative=True,
    )


def _extract_sv(raw_snap: str | bytes) -> int | None:
    """从存内快照 JSON 里抽 sv 作 int；失败/缺失返回 None（保守视作不可比则不放行拒写条件）。"""
    try:
        sv = json.loads(raw_snap).get("sv")
        return int(sv) if sv is not None else None
    except Exception:
        return None


async def invalidate_user_snap(user_id: uuid.UUID) -> None:
    """
    失效单用户快照 —— **A7 的调用口**：INCR epoch + DEL snap 原子一步，再清 L1 并广播。
    INCR 让改动前捕获旧 epoch 的在途回填在写时判不匹配拒写 → 缓存保持空、陈旧不复活；
    DEL 让缓存立即 miss → 下个读必经 DB 拉当前实况。Redis 不可用静默跳过。
    L1（本地内存）无法被 L2 DEL 波及，故 L2 提交成功后**先删本地 L1，再广播**让其他实例
    删各自 L1（订阅方只删 L1、不 DEL L2，避免误删他实例刚回填的新值）。
    """
    redis = await _get_redis(_snap_key(user_id))
    if redis is None:
        return
    key = _snap_key(user_id)
    ekey = _epoch_key(user_id)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.multi()
            pipe.incr(ekey)
            pipe.delete(key)
            await pipe.execute()
    except Exception:
        logger.debug("user_cache invalidate skip uid=%s", user_id)
        return
    if _l1_on():
        local_cache.l1_delete(key)
    # 即使本实例关闭 L1，也向其他实例广播失效。
    await user_cache_events.publish_invalidate(key)
