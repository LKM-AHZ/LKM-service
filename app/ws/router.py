"""WebSocket 实时端点：用户建立连接，订阅自己的若干推送通道。

浏览器 ``WebSocket`` 无法携带自定义请求头，鉴权改用握手 query 参数 ``token``
（短时效 access token）。校验复用 ``auth.seams.resolve_current_user`` 的完整语义
（用户存在/锁定/token_version/改密），会话自建自关（同 worker 模式）。

订阅通道由 query 参数 ``channels`` 指定（逗号分隔，缺省 ``upload``——保持 M6.7 之前
"连上即收上传登记事件"的旧行为，前端无需改动）。通道名走 broker 的服务端白名单，
且 Redis 通道的 user_id 前缀一律由服务端从 token 派生，**客户端无法订阅他人通道**。

连接建立后由 ``manager`` 统一登记与 Redis 订阅驱动。端点本身只维持连接、
感知对端断开，不要求客户端回业务消息。

蓝图 §5.7「断线重连 | 心跳(application ping/pong) + 重连 token 续期」的**服务端本分**：
服务端超时发 ``ping``，客户端回 ``pong`` 即刷新活跃；客户端可在连接存活期内发
``{"type":"refresh","token":"<新 access token>"}`` 续期——服务端复用握手同款鉴权校验，且
**新 token 的 user_id 必须与当前连接一致**，防止借续期把连接换成另一个用户。
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


async def _should_keep_connection(raw: str, user_id: uuid.UUID) -> bool:
    """处理一条客户端文本控制消息；返回是否保持连接（False = 立即关闭）。

    - ``{"type":"pong"}``：心跳应答，刷新活跃——**不**因"没收到业务消息"误判断线。
    - ``{"type":"refresh","token":...}``：复用握手 :func:`_authorize` 校验新 token；
      校验通过且 **user_id 与当前连接一致** 才续期；不一致/无效 → 关闭（fail-closed，
      杜绝借续期换身份）。续期成功后身份不变，故无需改动 manager 的订阅表。
    - 其它/非 JSON/非对象：沿用旧语义「收到文本即忽略」，保持连接。

    本函数只做**判定**（可单测）；实际 ``close`` 由调用方按既有未授权策略执行。
    """
    try:
        msg = json.loads(raw)
    except Exception:
        return True
    if not isinstance(msg, dict):
        return True
    mtype = msg.get("type")
    if mtype == "pong":
        return True
    if mtype == "refresh":
        token = msg.get("token")
        if not isinstance(token, str) or not token:
            logger.warning("ws refresh 缺 token，按未授权关闭 user=%s", user_id)
            return False
        new_uid = await _authorize(token)
        # 关键安全约束：续期只允许「同一用户换新 token」；不同 user_id 一律拒绝，
        # 否则续期就成了把当前连接身份换成他人的后门。
        if new_uid is None or new_uid != user_id:
            logger.warning("ws refresh 身份不符/无效，关闭 user=%s", user_id)
            return False
        return True
    return True


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
        # 只推送；循环接收以维持连接并感知对端断开。收到的文本按控制消息处理：
        # pong 刷新活跃、refresh 走同款鉴权续期，其余忽略（旧语义）。
        # 心跳：Starlette 无内置服务端 receive 超时，客户端静默断线时 receive_text()
        # 会无限挂起占住连接。这里用 wait_for 加窗，超时即发 ping 探活——对端已死时
        # send 抛错进入外层 except 清理，僵尸连接不再长期占内存。
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_HEARTBEAT_S
                )
            except TimeoutError:
                # 对端已死时 send 抛错 → break 触发 finally 清理
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    # 心跳失败即断连：不留痕的话，「socket 反复关闭」类问题无法定位
                    logger.debug("ws heartbeat ping failed, closing %s", user_id, exc_info=True)
                    break
                continue
            # 收到任意文本本身就刷新了活跃窗口；这里再按控制消息细化处理
            # （pong 保持、refresh 校验，续期失败/换身份则按既有未授权策略关闭）。
            if not await _should_keep_connection(raw, user_id):
                try:
                    await websocket.close(code=_UNAUTHORIZED_CLOSE)
                except Exception:
                    logger.debug("ws refresh 关闭连接失败 %s", user_id, exc_info=True)
                break
    except WebSocketDisconnect:
        pass
    finally:
        await manager.unregister(user_id, websocket)
