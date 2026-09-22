"""每用户只读快照缓存 `user:snap:{id}`：cache-through + 版本 CAS + 反陈旧防复活血统（M3.A.2/A6）。

这是 M3.A.2「cache-through + 版本 CAS + 失效防复活血统」的**缓存侧本分**：A7 的
auth 变更事件不在此接线；此处只把 A7 需要的失效原语（``invalidate_user_snap`` = del +
反陈旧哨兵 bump）导出供下一任务使用，并保证「失效后陈旧回填不可能把老数据卷回来」。

- **命名/日志/TTL**：沿用 ``app/core/cache.py`` 的 ``make_key`` + env 命名空间 ``lkm:{env}:*``、
  ``lkm.*`` DEBUG 日志、TTL 兜底、fail-open 语义。
- **fail-open**：Redis 未启用/不可用 → 一律静默返回/跳过，绝不抛错、不 crash 请求
  （调用方照常回退 DB）。
- **TTL 兜底**：快照键带短 TTL 防内存增长；失效代次哨兵(epoch)**无 TTL、常驻存活**——即便
  快照键因 TTL 过期 miss 重新回填，也须通过当前 epoch 校验，故不会用陈旧数据复活。

并发正确性设计（roadmap 硬性质：并发回填+失效，「老版本顶不回去；空(失效)后读必拉新」）
--------------------------------------------------------------------------

数据与权威分两条红线，各防一类竞态，两条都在 ``write_if_newer`` 的**原子**写里同时校验：

1) **来源版本 CAS（DB 新鲜度权威）**：快照值内嵌 ``sv`` = 来源版本（调用方由 User.updated_at
   推导的单调 int）。并发回填竞争写同一 user 时，只有「sv ≥ 已缓存 sv」通过——陈旧 sv 顶不
   掉更新的已缓存值（新世代胜；陈旧不覆盖新生代）。

2) **失效代次哨兵 epoch（失效权威，防复活）**：每 user 一枚**常驻、单调、仅失效 bump** 的
   ``epoch`` 键（``user:snap:ver:{id}``）。它**不像快照键那样被 del**，充当「幸存墓碑/
   monotonic high-water mark」，专堵 brief 指出的窗口：仅靠 ``del`` 时，一个已持有旧值+旧
   版本的在途回填能在 del 之后又把旧数据写回。解法 = 每次回填在发起 DB 读**之前**先捕获当前
   ``epoch``（``expected_epoch``），写时若 ``epoch`` 已被任何一次失效 bump（当前 ≠ 捕获）即
   拒写 → 缓存保持空 → 下一次读必然从 DB 拉到新值（无陈旧复活）。

逐用例推演（满足 roadmap 硬性质）：
- 两次回填、无失效：捕获同 epoch 均过二次校验；DB mid 变更致 A 读 V1、B 读 V2>V1，无论先后
  终值收敛最新 V2（B 先写 V2 则 A 的 V1 因 sv<已存被拒；A 先写 V1 则 B 的 V2 正常覆盖）。
- 失效 + 陈旧在途回填：A 停在 DB 读中（捕获 epoch=0），A7 失效 bump→1 并 del；A 回填带
  expected_epoch=0 ≠ 当前 1 被拒，缓存保持空 → 下个正常读拉回 DB 新值，旧值绝不复活。

注（已知局限，随 A7 前喂）：来源版本取 **User.updated_at**，而快照内容同时依赖 Profile；
同版本下 Profile 单独更新会产生不同内容却同版本。A6 无事件不处理（读时拉实况兜底），
**A7 事件失效必须对 User 与 Profile 两者变更同时 bump epoch** 以兜住此版本不敏感间隔。
compare 键用 updated_at 微秒作 int；同微秒多次变更=同版本为边界情形。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis import WatchError
from redis.asyncio import Redis as _AsyncRedis

import app.core.local_cache as local_cache
import app.core.redis as redis_client
import app.core.user_cache_events as user_cache_events
from app.core.cache import TTL_ITEM_S, make_key
from app.core.config import settings
from app.core.metrics import user_snap_cache_total

logger = logging.getLogger("lkm.user_cache")

# 失效代次哨兵键不存在时视作 epoch 0（首次失效 INCR 会 0 → 1，把捕获 0 的在途回填全拒掉）。
_EPOCH_ABSENT = 0
# 乐观锁（WATCH/MULTI CAS）重试上限：真并发竞态下的 WatchError 重试；超上限保守拒写（安全侧）。
_MAX_CAS_RETRY = 8


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
    """User.updated_at → 单调可比的来源版本 int（秒*1e6 + 微秒，归一 UTC 后取绝对刻度）。

    - 输入带/不带 tz 均先规整到 UTC 再取 ``timestamp*1e6 + tz-mic``，跨进程确定性可比、且随
      时刻单调不减。
    - 微秒为 compare 粒度：同微秒内多次变更同版本是已知边界局限（随 A7 双失效补齐，见 docstring）。
    """
    dt = updated_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000 + utc.microsecond


async def _get_redis() -> _AsyncRedis | None:
    return await redis_client.get_redis()


def _normalize_snap(data: dict[str, Any]) -> dict[str, Any]:
    """把快照 dict 内的 ``user_id`` 归一为 ``uuid.UUID``。

    Redis 里 uuid 以 JSON 字符串存放（写侧 :func:`write_if_newer` 用 ``default=str``），而
    DB 直读路径给出的是 ``uuid.UUID``；不归一会让「缓存命中」与「miss 回填」重建出的
    ``UserSnapshot.user_id`` 类型不一致（str vs UUID），下游按 id 比较随即失真。已是 UUID
    或本无该字段则原样返回。
    """
    uid = data.get("user_id")
    if isinstance(uid, str):
        try:
            return {**data, "user_id": uuid.UUID(uid)}
        except ValueError:
            return data
    return data


async def current_epoch(user_id: uuid.UUID) -> int:
    """读当前失效代次（快照读缝 DB 回填前调用、作为写时 expected_epoch）。fail-open→0。"""
    redis = await _get_redis()
    if redis is None:
        return _EPOCH_ABSENT
    try:
        raw = await redis.get(_epoch_key(user_id))
    except Exception:
        return _EPOCH_ABSENT
    return _EPOCH_ABSENT if raw is None else _to_int(raw)


def _to_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _EPOCH_ABSENT


async def read_snap(user_id: uuid.UUID) -> dict[str, Any] | None:
    """读快照数据 dict（L1 本地 → L2 Redis）；未命中/Redis 故障 → None（miss 由 DB 兜底）。

    L1 命中直接返回（免 L2 往返）；L1 miss 才查 L2，L2 命中后按 L1 TTL 回填本地。L1 条目
    仅作镜像，脏形态（非 dict）即删，不放大既有 ``_from_cache_dict`` 的脏缓存问题。

    **L1 回填不做 epoch 守卫（已知窗口，属设计取舍）**：本协程在 ``await redis.get(key)``
    期间若发生失效（另一实例 INCR epoch + DEL snap，本进程订阅任务删 L1 时 L1 尚为空），
    恢复后会把刚读到的旧值写进 L1，而 L1 命中不再看 L2/epoch → 该旧值可被服务至多
    ``user_snap_l1_ttl_s``（默认 10s）。要收紧需在 GET 之前与恢复之后各读一次 epoch
    （每次回填多两次 Redis 往返）并丢弃跨失效窗口的读值；鉴于 L1 的定位就是「TTL 有界的
    只读镜像、不具权威」（见模块 docstring）且默认 TTL 仅 10s，此处保留窗口并在此明示。
    同一模式亦见 :func:`read_snap_with_version` 与 :func:`read_snaps`。
    """
    redis = await _get_redis()
    if redis is None:
        return None
    key = _snap_key(user_id)
    if _l1_on():
        entry = local_cache.l1_get(key)
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict):
                user_snap_cache_total.labels("l1", "hit").inc()
                return _normalize_snap(data)
            local_cache.l1_delete(key)
        user_snap_cache_total.labels("l1", "miss").inc()
    try:
        raw = await redis.get(key)
    except Exception:
        logger.debug("user_cache get fail-open uid=%s", user_id)
        return None
    if raw is None:
        user_snap_cache_total.labels("l2", "miss").inc()
        logger.debug("user_cache miss uid=%s", user_id)
        return None
    user_snap_cache_total.labels("l2", "hit").inc()
    try:
        payload = json.loads(raw)
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        data = _normalize_snap(data)
        if _l1_on():
            sv = payload.get("sv")
            local_cache.l1_set(
                key,
                {"sv": _to_int(sv) if sv is not None else None, "data": data},
                settings.user_snap_l1_ttl_s,
            )
        return data
    except Exception:
        # 脏 L2 载荷（非 JSON / 非 dict）与「真 miss」在调用方看来都是 None，且此处已计过
        # l2 hit——不留日志的话，数据格式回归会表现为「命中率很高但一直回源」，无从排查
        logger.debug("user_cache payload 解析失败 uid=%s", user_id, exc_info=True)
        return None


async def read_snaps(user_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """批量读快照（L1 逐个 → L2 一次 MGET，命中回填 L1）；未命中的 id 不在结果里。

    语义与逐 id 调 :func:`read_snap` 等价（同一套 L1/L2 与命中指标），差别只在把 N 次 L2
    往返收成 1 次——M6.5 批量读（by-ids）的收益正是在此。Redis 不可用/异常 fail-open → 空
    （调用方走上游拉取），绝不抛错。
    """
    if not user_ids:
        return {}
    redis = await _get_redis()
    if redis is None:
        return {}
    l1 = _l1_on()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    pending: list[uuid.UUID] = []
    for uid in user_ids:
        if l1:
            entry = local_cache.l1_get(_snap_key(uid))
            if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
                user_snap_cache_total.labels("l1", "hit").inc()
                out[uid] = _normalize_snap(entry["data"])
                continue
            if entry is not None and not isinstance(entry, dict):
                local_cache.l1_delete(_snap_key(uid))  # 脏形态即删，不放大问题
            user_snap_cache_total.labels("l1", "miss").inc()
        pending.append(uid)
    if not pending:
        return out
    try:
        raws = await redis.mget([_snap_key(uid) for uid in pending])
    except Exception:
        logger.debug("user_cache mget fail-open n=%s", len(pending))
        return out
    for uid, raw in zip(pending, raws, strict=True):
        if raw is None:
            user_snap_cache_total.labels("l2", "miss").inc()
            continue
        user_snap_cache_total.labels("l2", "hit").inc()
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        data = _normalize_snap(data)
        out[uid] = data
        if l1:
            sv = payload.get("sv")
            local_cache.l1_set(
                _snap_key(uid),
                {"sv": _to_int(sv) if sv is not None else None, "data": data},
                settings.user_snap_l1_ttl_s,
            )
    return out


async def read_snap_with_version(
    user_id: uuid.UUID,
) -> tuple[int | None, dict[str, Any] | None]:
    """读缓存返回 `(sv, data)`（测试断言存内源版本用）；未命中/故障返回 `(None, None)`。

    同 :func:`read_snap` 走 L1 → L2，L2 命中回填 L1（信封含 sv，供版本断言）。
    """
    redis = await _get_redis()
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
            local_cache.l1_set(
                key, {"sv": sv, "data": data}, settings.user_snap_l1_ttl_s
            )
        return sv, data
    except Exception:
        return None, None


async def write_if_newer(
    user_id: uuid.UUID,
    data: dict[str, Any],
    source_version: int,
    expected_epoch: int,
) -> bool:
    """CAS 回填：sv 胜过已存值**且** epoch 未被失效 bump 才写入；否则拒写返回 False。

    原子实现 = WATCH[snap, epoch] + MULTI 乐观锁（repo M1.2 同款，fakeredis 可跑、生产零外部
    脚本依赖；不用 Lua/eval——fakeredis 不支持 eval/lupa）。陈旧/已失效拒写是**确定性**结果，
    直接返回不重试；只有 WatchError 代表的真并发竞态才乐观重试。
    """
    redis = await _get_redis()
    if redis is None:
        # fail-open：缓存不可用即「未写入」，调用方照常返回刚读到的 DB 值。
        return False

    key = _snap_key(user_id)
    ekey = _epoch_key(user_id)
    # default=str：快照 data 内的 user_id 是 uuid.UUID（DB 直读路径），JSON 无原生 uuid；
    # 序列化成 str 后由读侧 _normalize_snap 还原，保证 L1/L2 与 DB 读路径类型一致。
    value = json.dumps(
        {"sv": source_version, "data": data}, ensure_ascii=False, default=str
    )
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for _ in range(_MAX_CAS_RETRY):
                try:
                    await pipe.watch(key, ekey)
                    cur_epoch = _to_int(await pipe.get(ekey))
                    raw_cur_snap = await pipe.get(key)
                    # (1) 来源版本：不得以旧 sv 覆盖已缓存的更新值（等值视为幂等续写）
                    if raw_cur_snap is not None:
                        cur_sv = _extract_sv(raw_cur_snap)
                        if cur_sv is not None and cur_sv > source_version:
                            await pipe.reset()
                            return False
                    # (2) 失效代次：捕获 epoch != 当前 → 期间有失效，拒写防复活
                    if cur_epoch != expected_epoch:
                        await pipe.reset()
                        return False
                    pipe.multi()
                    pipe.set(key, value, ex=TTL_ITEM_S)
                    await pipe.execute()
                    # L2 CAS 成功（权威已接受）才镜像进 L1；拒绝/异常一律不碰 L1，
                    # 避免用陈旧值覆盖本地更新值。
                    if _l1_on():
                        local_cache.l1_set(
                            key,
                            {"sv": source_version, "data": data},
                            settings.user_snap_l1_ttl_s,
                        )
                    return True
                except WatchError:
                    await pipe.reset()
    except Exception:
        logger.exception("user_cache write CAS 异常，按未写入处理 uid=%s", user_id)
    return False


def _extract_sv(raw_snap: str) -> int | None:
    """从存内快照 JSON 里抽 sv 作 int；失败/缺失返回 None（保守视作不可比则不放行拒写条件）。"""
    try:
        sv = json.loads(raw_snap).get("sv")
        return int(sv) if sv is not None else None
    except Exception:
        return None


async def invalidate_user_snap(user_id: uuid.UUID) -> None:
    """失效单用户快照 —— **A7 的调用口**：INCR epoch + DEL snap 原子一步，再清 L1 并广播。

    INCR 让改动前捕获旧 epoch 的在途回填在写时判不匹配拒写 → 缓存保持空、陈旧不复活；
    DEL 让缓存立即 miss → 下个读必经 DB 拉当前实况。Redis 不可用静默跳过。

    L1（本地内存）无法被 L2 DEL 波及，故 L2 提交成功后**先删本地 L1，再广播**让其他实例
    删各自 L1（订阅方只删 L1、不 DEL L2，避免误删他实例刚回填的新值）。
    """
    redis = await _get_redis()
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
    # 广播不受本实例 L1 开关约束：它服务的是**其它**实例的 L1——本机 user_snap_l1_enabled
    # 为 false（滚动发布/配置不一致）不代表对端也关，漏发会让对端继续命中陈旧 L1 直到 TTL 过期
    await user_cache_events.publish_invalidate(key)
