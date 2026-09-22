"""WebSocket 实时端点：用户建立连接，订阅自己的若干推送通道。

浏览器 ``WebSocket`` 无法携带自定义请求头，鉴权改用握手 query 参数 ``token``
（短时效 access token）。校验复用 ``auth.seams.resolve_current_user`` 的完整语义
（用户存在/锁定/token_version/改密），会话自建自关（同 worker 模式）。

订阅通道由 query 参数 ``channels`` 指定（逗号分隔，缺省 ``upload``——保持 M6.7 之前
"连上即收上传登记事件"的旧行为，前端无需改动）。通道名走 broker 的服务端白名单，
且 Redis 通道的 user_id 前缀一律由服务端从 token 派生，**客户端无法订阅他人通道**。

连接建立后由 ``manager`` 统一登记与 Redis 订阅驱动。端点本身只维持连接、
感知对端断开，不要求客户端回业务消息。
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.err import BizError
from app.db.session import new_session
from app.ws.broker import CHANNEL_UPLOAD, CHANNELS
from app.ws.manager import manager
from auth.seams import resolve_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["ws"])

# 校验失败的私有 close code（沿用业界常见 4xxx 保留段）
_UNAUTHORIZED_CLOSE = 4401
_BAD_REQUEST_CLOSE = 4400

# 心跳间隔：超过此窗口未收到任何对端消息，则发一条 ping 探活；
# 若对端已静默断线（NAT 过期/断网），send 会抛错从而清理僵尸连接，避免长期占内存。
_HEARTBEAT_S = 30.0


async def _authorize(token: str) -> uuid.UUID | None:
    """校验 access token，返回 user_id；缺失/无效返回 None。"""
    if not token:
        return None
    db = await new_session()
    try:
        cur = await resolve_current_user(token, db)
    except BizError:
        # 真正的鉴权失败：按未授权处理
        return None
    except Exception:
        # 基础设施故障（DB/seam 异常）也返回 None → 客户端只会看到 4401「未授权」，
        # 运维毫无信号，排障时与「凭据错」无法区分，故必须留痕
        logger.exception("ws 鉴权出现非鉴权类异常（按未授权关闭）")
        return None
    finally:
        await db.close()
    return cur.id


def _parse_channels(raw: str) -> tuple[str, ...] | None:
    """解析 ``channels`` query 参数；返回订阅通道元组，非法通道返回 None。

    缺省（空串）→ 仅 ``upload``（兼容旧前端）；重复去重、保序。
    """
    if not raw.strip():
        return (CHANNEL_UPLOAD,)
    channels: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in CHANNELS:
            return None
        if name not in channels:
            channels.append(name)
    return tuple(channels) if channels else (CHANNEL_UPLOAD,)


@router.websocket("/events")
async def ws_events(websocket: WebSocket) -> None:
    """实时连接：`?token=<access>&channels=upload,notify` 鉴权成功则按通道推送。"""
    token = websocket.query_params.get("token", "")
    user_id = await _authorize(token)
    if user_id is None:
        await websocket.close(code=_UNAUTHORIZED_CLOSE)
        return

    channels = _parse_channels(websocket.query_params.get("channels", ""))
    if channels is None:
        await websocket.close(code=_BAD_REQUEST_CLOSE)
        return

    await websocket.accept()
    try:
        # 登记与订阅驱动也放进 try：此处抛错时连接已 accept，若不清理会永久留在
        # manager 表里（unregister 幂等，未登记过也能安全调用）。
        await manager.register(user_id, websocket, channels)
        await manager.ensure_subscription()
        # 只推送；循环接收以维持连接并感知对端断开（收到文本即忽略）。
        # 心跳：Starlette 无内置服务端 receive 超时，客户端静默断线时 receive_text()
        # 会无限挂起占住连接。这里用 wait_for 加窗，超时即发 ping 探活——对端已死时
        # send 抛错进入外层 except 清理，僵尸连接不再长期占内存。
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                # 对端已死时 send 抛错 → break 触发 finally 清理
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await manager.unregister(user_id, websocket)
