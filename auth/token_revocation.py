"""access token 的 jti 撤销快速预检（L2 黑名单）。

蓝图 §4.2/§5.6：撤销状态**权威在 DB**，L2 只做**快速预检**——命中必拒，未命中仍回查 DB
权威判据；Redis 不可用时静默跳过预检（fail-open），由 DB 判据兜底，绝不把缓存抖动变成误拒
全站会话。

与 ``token_version`` 的分工：``token_version`` 是「该用户全部会话」的全局判据（改密/封号/
全端登出），jti 黑名单只废**单枚 token**（单设备登出）。故 jti 只做「加拒」——任何路径都
不得用它取代 DB 权威判定。

键 ``jti:block:{jti}``，TTL = token 剩余有效期（access 上限 15min，条目因此自洁）。auth
进程与业务进程连同一个 L2（同一 ``settings.redis_url``），键空间共享、写一侧两侧可见。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.redis import get_redis

logger = logging.getLogger("lkm.auth.token_revocation")

_PREFIX = "jti:block:"
# 兜底 TTL：取不到 exp 时用 access token 上限，避免黑名单条目永不过期而无限累积
_MAX_TTL_S = 900


def _key(jti: str) -> str:
    return f"{_PREFIX}{jti}"


def remaining_ttl_seconds(payload: dict[str, Any]) -> int:
    """按 token 的 ``exp`` 算剩余寿命（秒），给写黑名单做 TTL；取不到就用上限兜底。"""
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return _MAX_TTL_S
    return max(1, min(int(exp - time.time()), _MAX_TTL_S))


async def block_jti(jti: str | None, ttl_seconds: int) -> bool:
    """把 jti 写入黑名单，返回是否写入成功。

    失败（Redis 未配置/不可达）**不抛**：调用方据此回退 DB 权威手段（bump ``token_version``），
    保证登出确实生效——不能让一次缓存故障变成「登出没生效却报成功」。
    """
    if not jti:
        return False
    ttl = max(1, min(int(ttl_seconds), _MAX_TTL_S))
    try:
        client = await get_redis()
        if client is None:
            return False
        # 用 set(..., ex=) 而非 setex：后者在 redis-py 新版已弃用，本仓 filterwarnings=error
        # 下会直接抛 DeprecationWarning（被下面的兜底吞掉 → 黑名单静默写不进去）。
        await client.set(_key(jti), "1", ex=ttl)
        return True
    except Exception:
        logger.warning("jti 黑名单写入失败 jti=%s", jti, exc_info=True)
        return False


async def block_payload_jti(payload: dict[str, Any]) -> bool:
    """按已解码的 token payload 写黑名单（TTL 取该 token 的剩余寿命）。返回是否写入成功。

    登出路径的统一入口：payload 无 jti（灰度期旧 token）视为无需处理，返回 False。
    """
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        return False
    return await block_jti(jti, remaining_ttl_seconds(payload))


async def is_jti_blocked(jti: str | None) -> bool:
    """该 jti 是否已被撤销：**命中必拒**。

    Redis 不可用/异常时返回 False（跳过预检，交由 DB 权威判据兜底）；**无 jti 的旧 token 一律
    返回 False**——灰度期内仍有 jti 之前签发的 token 在有效期内，不能因取不到标识就全数拒绝。
    """
    if not jti:
        return False
    try:
        client = await get_redis()
        if client is None:
            return False
        return bool(await client.exists(_key(jti)))
    except Exception:
        logger.warning("jti 黑名单查询失败 jti=%s", jti, exc_info=True)
        return False
