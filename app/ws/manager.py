"""API 进程侧：持有 WebSocket 连接，订阅 Redis 通道并扇出给对应 user 的对应通道。

worker 进程只 ``publish``（见 broker.py），不持有连接；连接唯一存在于 API 进程（本模块）。
Redis 订阅用常驻后台 task（幂等懒启动，首次 WS 连接时拉起），集中 ``psubscribe ws:*``
再按 ``ws:{user_id}:{channel}`` 解析出 (user_id, channel) 扇出 —— 避免每连接一条 sub 连接。

M6.7 泛化：连接表从「按 user_id」扩为「按 user_id → channel」，一次连接可订阅多个通道
（端点侧按白名单校验，见 router.py）。多 worker/多副本下每个进程各持一份连接表、各自
订阅同一模式，故扇出正确性完全依赖 Redis 广播（不共享进程内状态）。
"""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Protocol

from app.core.redis import get_redis
from app.ws.broker import CHANNEL_UPLOAD, parse_channel


class Dispatcheable(Protocol):
    """Manager 只依赖的推送接口：任意能 ``await send_text(str)`` 的连接均可注册。"""

    async def send_text(self, data: str) -> None: ...


class ConnectionManager:
    """``user_id -> channel -> 连接集合`` 的活动连接表 + Redis 订阅驱动的扇出。"""

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, dict[str, set[Dispatcheable]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._lock = asyncio.Lock()
        self._sub_task: asyncio.Task[Any] | None = None
        self._start_lock = asyncio.Lock()

    async def register(
        self,
        user_id: uuid.UUID,
        ws: Dispatcheable,
        channels: Iterable[str] = (CHANNEL_UPLOAD,),
    ) -> None:
        async with self._lock:
            for channel in channels:
                self._connections[user_id][channel].add(ws)

    async def unregister(self, user_id: uuid.UUID, ws: Dispatcheable) -> None:
        """摘除连接的全部通道订阅（连接对象不记通道，故遍历）——幂等。"""
        async with self._lock:
            chans = self._connections.get(user_id)
            if not chans:
                return
            for channel in list(chans):
                chans[channel].discard(ws)
                if not chans[channel]:
                    chans.pop(channel, None)
            if not chans:
                self._connections.pop(user_id, None)

    async def dispatch(self, user_id: uuid.UUID, channel: str, message: str) -> None:
        """向某用户某通道的所有连接推送同一文本消息。失效连接尽力移除，不阻塞整体。"""
        async with self._lock:
            targets: list[Dispatcheable] = list(
                self._connections.get(user_id, {}).get(channel, ())
            )
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                async with self._lock:
                    chans = self._connections.get(user_id)
                    if chans:
                        chans.get(channel, set()).discard(ws)

    # ---- Redis 订阅驱动（生命周期）----

    async def ensure_subscription(self) -> None:
        """幂等启动全局订阅 task；已运行/启动中则跳过。"""
        if self._sub_task is not None and not self._sub_task.done():
            return
        async with self._start_lock:
            if self._sub_task is not None and not self._sub_task.done():
                return
            self._sub_task = asyncio.create_task(self._sub_loop())

    async def _sub_loop(self) -> None:
        """常驻：psubscribe ``ws:*`` → 解析 (user_id, channel) → dispatch。

        非 ``ws:{user_id}:{channel}`` 或通道不在白名单的消息丢弃（见 broker.parse_channel）。
        Redis 未就绪就退避重试；订阅连接异常同样退避重连。取消即退出。
        """
        while True:
            redis = await get_redis()
            if redis is None:
                await asyncio.sleep(1)
                continue
            pubsub = redis.pubsub()
            try:
                await pubsub.psubscribe("ws:*")
                while True:
                    msg: dict[str, Any] | None = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if msg is None:
                        continue
                    if msg.get("type") != "pmessage":
                        continue
                    channel = msg.get("channel")
                    data = msg.get("data")
                    if isinstance(data, bytes):
                        data = data.decode()
                    if not isinstance(channel, str) or not isinstance(data, str):
                        continue
                    parsed = parse_channel(channel)
                    if parsed is None:
                        continue
                    await self.dispatch(parsed[0], parsed[1], data)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 订阅链路异常：关连接后退避重连，保持常驻
                with suppress(Exception):
                    await pubsub.aclose()
                await asyncio.sleep(1)

    async def close(self) -> None:
        """收尾：取消订阅 task，避免泄漏 Redis 连通。幂等。"""
        task, self._sub_task = self._sub_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# 全局唯一管理器实例（进程内共享）
manager = ConnectionManager()
