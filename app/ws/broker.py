"""事件发布侧：worker 进程把事件发布到 Redis pub/sub，供 API 进程转发给 WebSocket。

通道命名约定 ``ws:{user_id}:{channel}``（M6.7 泛化；泛化前为 ``ws:upload:{uploader_id}``）。
**user_id 前缀由服务端从 token 派生**，客户端不能自选，故越权订阅面天然收敛于通道白名单
（见 ``CHANNELS``，订阅侧同表校验）。

现有通道：
- ``upload``：直传登记完成（原 ``ws:upload:<id>`` 语义与会话端不变）；
- ``notify``：站内信/业务通知（M6.8 使用）。

推送 body 统一带两个幂等/排序字段：
- ``event_id``：事件幂等键。调用方可显式传入（如复用 outbox 事件的 event_id），未传则自动
  生成 UUID——即「新事件」语义。同一 ``event_id`` 重推时 body 逐字段一致，前端据此去重。
- ``version``：调用方给的单调版本号（缺省 0 = 未指定）。前端可据 ``(event_id, version)``
  丢弃旧帧。

发布一律 fail-open（``app.core.redis`` 语义）：Redis 不可用或 publish 异常静默 no-op，
广播只是体验增强，缺失时前端回退到「稍后刷新」即可，不该阻塞登记/通知主流程。
"""

import json
import logging
import uuid
from typing import Any

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

CHANNEL_UPLOAD = "upload"
CHANNEL_NOTIFY = "notify"

# 服务端通道白名单：发布侧与订阅侧共用（越权订阅/投递的唯一收敛点）
CHANNELS: frozenset[str] = frozenset({CHANNEL_UPLOAD, CHANNEL_NOTIFY})

_CHANNEL_PREFIX = "ws"


def ws_channel(user_id: uuid.UUID, channel: str) -> str:
    """返回某用户某通道的 Redis 通道名（``ws:{user_id}:{channel}``）。"""
    return f"{_CHANNEL_PREFIX}:{user_id}:{channel}"


def upload_channel(uploader_id: uuid.UUID) -> str:
    """上传通道名（兼容旧调用点）。"""
    return ws_channel(uploader_id, CHANNEL_UPLOAD)


def parse_channel(channel: str) -> tuple[uuid.UUID, str] | None:
    """解析 ``ws:{user_id}:{channel}``；格式不符或通道不在白名单返回 None。"""
    parts = channel.split(":", 2)
    if len(parts) != 3 or parts[0] != _CHANNEL_PREFIX:
        return None
    try:
        user_id = uuid.UUID(parts[1])
    except ValueError:
        return None
    if parts[2] not in CHANNELS:
        return None
    return user_id, parts[2]


async def publish(
    user_id: uuid.UUID,
    channel: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    version: int | None = None,
) -> None:
    """向 ``ws:{user_id}:{channel}`` 发布一条消息（fail-open，异常/未配置静默）。"""
    if channel not in CHANNELS:
        # 白名单外：调用方 bug，静默丢弃优于越权投递
        return
    redis = await get_redis()
    if redis is None:
        return
    body = dict(payload)
    # 幂等键优先保留调用方（或 payload 里）已给的值：前端靠 event_id/version 去重，
    # 无条件覆盖会丢掉重发事件的原标识。显式参数 > payload 自带 > 新生成/默认。
    if event_id is not None:
        body["event_id"] = event_id
    else:
        body.setdefault("event_id", str(uuid.uuid4()))
    if version is not None:
        body["version"] = version
    else:
        body.setdefault("version", 0)
    try:
        data = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        # 序列化失败属调用方编程错误（payload 含不可 JSON 序列化对象），不得与
        # 「Redis 不可用」的 fail-open 混为一谈：留日志并抛出，让问题暴露
        logger.warning(
            "ws publish payload 不可序列化: channel=%s",
            ws_channel(user_id, channel),
            exc_info=True,
        )
        raise
    try:
        await redis.publish(ws_channel(user_id, channel), data)
    except Exception:
        # 广播失败不影响主流程；前端靠超时/刷新兜底
        logger.warning(
            "ws publish failed: channel=%s",
            ws_channel(user_id, channel),
            exc_info=True,
        )
        return


async def publish_upload_bound(
    uploader_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    version: int | None = None,
) -> None:
    """把登记完成的 payload 发布到该 uploader 的 upload 通道（可带幂等键）。"""
    await publish(
        uploader_id, CHANNEL_UPLOAD, payload, event_id=event_id, version=version
    )


async def publish_notification(
    user_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    version: int | None = None,
) -> None:
    """把通知 payload 发布到该用户的 notify 通道（M6.8 生产侧入口）。"""
    await publish(user_id, CHANNEL_NOTIFY, payload, event_id=event_id, version=version)
