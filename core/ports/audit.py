"""
审计端口：写审计日志、导出审计到 ClickHouse、开 auth realm 会话。
实现由 auth 绑定。会话类型对 core 不透明，故签名里的 ``db``/``client`` 用 ``Any``
（core 不依赖具体会话/客户端实现）。
"""

from __future__ import annotations

import uuid
from typing import Any

from core.ports.registry import get


async def log_audit(
    db: Any,
    user_id: uuid.UUID | None,
    action: str,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    """创建一条审计日志记录（**调用方必须已持有 auth realm 会话**）。"""
    await get("audit").log_audit(
        db, user_id, action, detail=detail, ip_address=ip_address
    )


async def record_audit(
    db: Any,
    user_id: uuid.UUID | None,
    action: str,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    """业务域写审计：内部按部署形态走 AUTH internal 端点或同库直写。

    与 :func:`log_audit` 的区别：本函数面向**业务库会话**（``core.db.session.get_session``），
    实现方负责跨 realm；直接把业务会话传给 :func:`log_audit` 会在拆库后
    ``UndefinedTable``（``audit_logs`` 属 ``AuthBase``）。失败是 **fail-closed** 的：
    抛 ``BizError(UNAVAILABLE)``，调用方业务事务应随之回滚。
    """
    await get("audit").record_audit(
        db, user_id, action, detail=detail, ip_address=ip_address
    )


async def export_audit_logs(db: Any, client: Any, *, window: int) -> int:
    """把 auth 库 audit_logs 导出到 ClickHouse，返回导出条数。"""
    return await get("audit").export_audit_logs(db, client, window=window)


async def new_auth_session() -> Any:
    """新开一个 auth realm 会话（调用方负责 close）；非请求上下文用。"""
    return await get("audit").new_auth_session()
