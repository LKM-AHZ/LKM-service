"""任务入队封装：消息总线可用则入队；否则降级同步发送（fail-open，不丢）。

对象事件通知入队为 fire-and-forget：无同步等价物，消息总线不可用时静默 no-op
（事件侧可经确认/重建恢复，遇故障宁可丢弃也不阻塞回调 200 时序）。

routing_key 常量统一定义在 ``core.messaging``（唯一事实源），此处 re-export 供既有
业务模块 import。
"""

import asyncio
import logging
from typing import Any

from app.core import messaging
from app.core.messaging import (
    RKEY_NOTIFY,
    RKEY_POINTS,
    RKEY_SEND_CODE,
    RKEY_SEND_MAGIC,
    RKEY_USER_BANNED,
    RKEY_USER_SESSION_REVOKE,
    RKEY_USER_UPDATED,
)

logger = logging.getLogger("lkm.jobs")

__all__ = [
    "RKEY_NOTIFY",
    "RKEY_POINTS",
    "RKEY_SEND_CODE",
    "RKEY_SEND_MAGIC",
    "RKEY_USER_BANNED",
    "RKEY_USER_SESSION_REVOKE",
    "RKEY_USER_UPDATED",
    "enqueue_upload_notify",
    "send_code",
    "send_magic_link",
]

# 降级同步发送的上限时长（同旧实现）。
_SEND_TIMEOUT_S = 10.0


async def _enqueue(fn: str, *args: Any, routing_key: str) -> bool:
    """发 JSON 消息到消息总线。不可用/异常返回 False（由调用方降级）。"""
    try:
        return await messaging.publish(routing_key, {"fn": fn, "args": list(args)})
    except Exception:
        # messaging.publish 内已 fail-open 捕获异常返回 False；此处兜底以防极少数
        # 直接抛出的情况（如测试注入），保持一致：异常视为入队失败 → 降级。
        logger.exception("enqueue %s failed", fn)
        return False


async def _degraded_send(coro_factory: Any, *, kind: str) -> None:
    try:
        await asyncio.wait_for(coro_factory(), timeout=_SEND_TIMEOUT_S)
    except TimeoutError:
        logger.warning("degraded %s send timed out after %ss", kind, _SEND_TIMEOUT_S)
    except Exception:
        logger.exception("degraded %s send failed", kind)


async def send_code(channel_key: str, contact: str, code: str) -> None:
    if await _enqueue(
        "send_code", channel_key, contact, code, routing_key=RKEY_SEND_CODE
    ):
        return
    from auth.seams import get_channel

    await _degraded_send(
        lambda: get_channel(channel_key).send_code(contact, code), kind="code"
    )


async def send_magic_link(email: str, link: str) -> None:
    if await _enqueue("send_magic_link", email, link, routing_key=RKEY_SEND_MAGIC):
        return
    from auth.deps import get_email_provider

    await _degraded_send(
        lambda: get_email_provider().send_magic_link(email, link), kind="magic_link"
    )


async def enqueue_upload_notify(upload_id: str) -> bool:
    return await _enqueue("notify_upload", upload_id, routing_key=RKEY_NOTIFY)
