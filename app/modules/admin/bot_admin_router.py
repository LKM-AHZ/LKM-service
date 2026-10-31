"""后台「机器人」原生页面数据端点（BFF）。

背景：``/admin/bot/*`` 目前以同源 iframe 内嵌 LKMBot 面板，免登走 60s 一次性 SSO 票据。
原生页面不再让浏览器直连 bot，而是由 LKM-service 以**服务身份**取数：

- 浏览器只与本站同域通信并携带后台 cookie；
- 服务侧用 ``bot_admin_api_key``（LKM-bot OpenAPI v1）调用面板接口；
- 每个端点先过 ``require_admin``，再要求 ``admin.bot_panel`` 能力点——与换票端点同一门禁，
  社区 admin 会话本身不等于 Bot 控制面授权。

失败语义：bot 未配置 / 不可达 / 返回错误时**不抛 5xx**，而是返回
``configured`` / ``available`` 标记，页面据此降级为「面板未接入」并保留 iframe 兜底；
监控数据不可用不应连带后台整页失败。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rbac.permissions import Permission
from core.client_ip import client_ip
from core.common import ApiResp
from core.config import settings
from core.contracts import CurrentUser
from core.db.session import get_session
from core.err import BizError, CommonErr, respond
from core.ports.audit import record_audit
from core.secrets import reveal

from .deps import require_admin
from .permissions import require_permission
from .schemas import (
    AdminBotEnabledPatch,
    AdminBotList,
    AdminBotListItem,
    AdminBotLogHistory,
    AdminBotOverview,
    AdminBotPipInstallRequest,
    AdminBotPlatformItem,
    AdminBotPlatforms,
    AdminBotProviderItem,
    AdminBotProviders,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/bot", tags=["admin-bot"])


def _bot_configured() -> bool:
    """服务端是否配齐 bot 地址与服务密钥。"""
    return bool(
        settings.bot_api_base_url.strip() and reveal(settings.bot_admin_api_key)
    )


def _bot_client() -> httpx.AsyncClient:
    """构造访问 LKM-bot OpenAPI v1 的客户端（Bearer 服务密钥）。

    Returns:
        已带 base_url 与鉴权头的异步客户端；调用方负责 ``async with`` 关闭。
    """
    return httpx.AsyncClient(
        base_url=settings.bot_api_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {reveal(settings.bot_admin_api_key)}"},
        timeout=settings.bot_admin_timeout_s,
    )


async def _bot_get(client: httpx.AsyncClient, path: str) -> Any:
    """GET 一个 bot v1 端点并解开 ``{status, message, data}`` 信封。

    Args:
        client: 已配置 base_url/鉴权头的 bot 客户端。
        path: 相对 v1 根的路径（如 ``/stats``）。

    Returns:
        信封里的 ``data``（可能为 dict / list）。

    Raises:
        httpx.HTTPError: 网络或非 2xx 响应。
        ValueError: 响应不是对象信封，或 ``status != "ok"``。
    """
    resp = await client.get(path)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("bot response is not an object")
    if payload.get("status") != "ok":
        raise ValueError(str(payload.get("message") or "bot request failed"))
    return payload.get("data")


def _size(value: Any, key: str) -> int | None:
    """从 bot 返回的 dict/list 里取条目数（不同端点分别返回两种形态）。"""
    if isinstance(value, dict):
        items = value.get(key)
        if isinstance(items, list):
            return len(items)
        return None
    if isinstance(value, list):
        return len(value)
    return None


@router.get("/overview", response_model=ApiResp[AdminBotOverview])
@respond
async def admin_bot_overview(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotOverview:
    """返回后台概览卡片所需的 bot 运行数据。

    Args:
        cur: 当前后台管理员（cookie 会话，seam 裁决）。
        db: 业务库会话，用于判定 ``admin.bot_panel`` 能力点。

    Returns:
        聚合后的概览；未配置或取数失败时带 ``configured`` / ``available`` 标记。

    Raises:
        BizError: 缺少 ``admin.bot_panel`` 能力点（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotOverview(
            configured=False, available=False, reason="not_configured"
        )

    try:
        async with _bot_client() as client:
            stats, version, providers, plugins = await asyncio.gather(
                _bot_get(client, "/stats"),
                _bot_get(client, "/stats/version"),
                _bot_get(client, "/providers"),
                _bot_get(client, "/plugins"),
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot overview unavailable: %s", exc)
        return AdminBotOverview(configured=True, available=False, reason="unavailable")

    stats_data = stats if isinstance(stats, dict) else {}
    version_data = version if isinstance(version, dict) else {}
    raw_memory = stats_data.get("memory")
    memory = raw_memory if isinstance(raw_memory, dict) else {}
    raw_running = stats_data.get("running")
    running = raw_running if isinstance(raw_running, dict) else None
    return AdminBotOverview(
        configured=True,
        available=True,
        version=str(version_data.get("version") or "") or None,
        dashboard_version=str(version_data.get("dashboard_version") or "") or None,
        platform_count=stats_data.get("platform_count")
        if isinstance(stats_data.get("platform_count"), int)
        else None,
        provider_count=_size(providers, "providers"),
        plugin_count=_size(plugins, "plugins"),
        message_count=stats_data.get("message_count")
        if isinstance(stats_data.get("message_count"), int)
        else None,
        cpu_percent=stats_data.get("cpu_percent")
        if isinstance(stats_data.get("cpu_percent"), (int, float))
        else None,
        memory_process_mb=memory.get("process")
        if isinstance(memory.get("process"), int)
        else None,
        memory_system_mb=memory.get("system")
        if isinstance(memory.get("system"), int)
        else None,
        thread_count=stats_data.get("thread_count")
        if isinstance(stats_data.get("thread_count"), int)
        else None,
        running=running,
    )


_ACTION_PIP_INSTALL = "admin_bot_pip_install"


@router.get("/logs/history", response_model=ApiResp[AdminBotLogHistory])
@respond
async def admin_bot_logs_history(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotLogHistory:
    """回填日志页打开时已有的最近日志（bot `/logs/history` 的服务侧代理）。

    Args:
        cur: 当前后台管理员。
        db: 业务库会话，用于判定 ``admin.bot_panel`` 能力点。

    Returns:
        历史日志；未配置或不可达时以 ``configured`` / ``available`` 降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel`` 能力点（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotLogHistory(
            configured=False, available=False, reason="not_configured"
        )
    try:
        async with _bot_client() as client:
            data = await _bot_get(client, "/logs/history")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot logs history unavailable: %s", exc)
        return AdminBotLogHistory(
            configured=True, available=False, reason="unavailable"
        )
    logs = data.get("logs") if isinstance(data, dict) else None
    return AdminBotLogHistory(
        configured=True,
        available=True,
        logs=[entry for entry in logs if isinstance(entry, dict)]
        if isinstance(logs, list)
        else [],
    )


@router.get("/logs/live")
async def admin_bot_logs_live(
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """透传 bot 的 SSE 实时日志（浏览器用同源 cookie 连接）。

    Args:
        request: 入站请求，用于转发 ``Last-Event-ID``。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        ``text/event-stream`` 流；未配置时 503（页面据此回退完整面板）。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 拒绝/不可达（503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")

    headers = {}
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    client = _bot_client()
    try:
        upstream = await client.send(
            client.build_request("GET", "/logs/live", headers=headers),
            stream=True,
        )
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("admin bot logs live unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="bot panel is unavailable") from exc

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/logs/export")
async def admin_bot_logs_export(
    redact: bool = True,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """以服务身份导出诊断日志 ZIP（浏览器同域下载，不暴露 bot 密钥）。

    Args:
        redact: 是否脱敏（默认是）。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        ZIP 流；未配置时 503。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 不可达（503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")

    client = _bot_client()
    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                "/logs/export",
                params={"redact": "true" if redact else "false"},
            ),
            stream=True,
        )
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("admin bot logs export unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="bot panel is unavailable") from exc

    filename = upstream.headers.get("content-disposition", "")

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": filename or 'attachment; filename="lkmbot-logs.zip"',
        },
    )


@router.post("/pip/install", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_bot_pip_install(
    payload: AdminBotPipInstallRequest,
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """代理 bot 的 pip 安装（日志页的运维动作），成功与拒绝都写审计。

    Args:
        payload: 包名与可选镜像。
        request: 入站请求，用于审计记录来源 IP。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        bot 返回的 data 信封内容。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 不可达（502/503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")

    body = payload.model_dump(exclude_none=True)
    try:
        async with _bot_client() as client:
            resp = await client.post("/pip/install", json=body)
            resp.raise_for_status()
            envelope = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot pip install failed: %s", exc)
        raise HTTPException(status_code=502, detail="bot pip install failed") from exc

    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        message = envelope.get("message") if isinstance(envelope, dict) else None
        raise BizError(CommonErr.UNAVAILABLE, str(message or "bot pip install failed"))

    await record_audit(
        db,
        cur.id,
        _ACTION_PIP_INSTALL,
        detail=f"package={body.get('package', '')}",
        ip_address=client_ip(request),
    )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


_ACTION_PLATFORM_TOGGLE = "admin_bot_platform_toggle"
_ACTION_PROVIDER_TOGGLE = "admin_bot_provider_toggle"


@router.get("/platforms", response_model=ApiResp[AdminBotPlatforms])
@respond
async def admin_bot_platforms(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotPlatforms:
    """平台实例列表（名称取 bot-types 的 display_name）。

    Args:
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        平台列表；未配置或不可达时以 configured/available 降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotPlatforms(
            configured=False, available=False, reason="not_configured"
        )
    try:
        async with _bot_client() as client:
            bots, types = await asyncio.gather(
                _bot_get(client, "/bots"),
                _bot_get(client, "/bot-types"),
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot platforms unavailable: %s", exc)
        return AdminBotPlatforms(configured=True, available=False, reason="unavailable")

    bot_items = bots.get("bots") if isinstance(bots, dict) else None
    type_items = types.get("bot_types") if isinstance(types, dict) else None
    names = {
        str(item.get("type")): str(item.get("display_name") or item.get("type"))
        for item in type_items or []
        if isinstance(item, dict)
    }
    items = []
    for bot in bot_items or []:
        if not isinstance(bot, dict):
            continue
        bot_id = str(bot.get("id") or "")
        if not bot_id:
            continue
        bot_type = bot.get("type")
        items.append(
            AdminBotPlatformItem(
                id=bot_id,
                type=str(bot_type) if bot_type else None,
                display_name=names.get(str(bot_type)) if bot_type else None,
                enabled=bool(bot.get("enable", False)),
            )
        )
    return AdminBotPlatforms(configured=True, available=True, items=items)


@router.patch("/platforms/{bot_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_bot_platform_toggle(
    bot_id: str,
    payload: AdminBotEnabledPatch,
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """启用/停用一个平台实例（代理 bot，成功后写审计）。

    Args:
        bot_id: 平台实例 id。
        payload: 目标启用状态。
        request: 入站请求，用于审计记录来源 IP。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        bot 返回的 data。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 不可达（503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")
    try:
        async with _bot_client() as client:
            resp = await client.patch(
                f"/bots/{quote(bot_id, safe='')}/enabled",
                json={"enabled": payload.enabled},
            )
            resp.raise_for_status()
            envelope = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot platform toggle failed: %s", exc)
        raise HTTPException(status_code=503, detail="bot panel is unavailable") from exc
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        raise BizError(CommonErr.UNAVAILABLE, "bot panel rejected the update")
    await record_audit(
        db,
        cur.id,
        _ACTION_PLATFORM_TOGGLE,
        detail=f"bot={bot_id} enabled={payload.enabled}",
        ip_address=client_ip(request),
    )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


@router.get("/providers", response_model=ApiResp[AdminBotProviders])
@respond
async def admin_bot_providers(
    capability: str | None = None,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotProviders:
    """模型提供商列表（可按 capability 过滤）。

    Args:
        capability: chat/stt/tts/embedding/rerank，缺省取全部。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        提供商列表；未配置或不可达时降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotProviders(
            configured=False, available=False, reason="not_configured"
        )
    path = "/providers"
    if capability:
        path = f"{path}?capability={quote(capability, safe='')}"
    try:
        async with _bot_client() as client:
            data = await _bot_get(client, path)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot providers unavailable: %s", exc)
        return AdminBotProviders(configured=True, available=False, reason="unavailable")

    raw_items = data.get("providers") if isinstance(data, dict) else None
    items = []
    for provider in raw_items or []:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "")
        if not provider_id:
            continue
        provider_type = provider.get("provider_type") or provider.get("type")
        model = provider.get("model")
        items.append(
            AdminBotProviderItem(
                id=provider_id,
                type=str(provider_type) if provider_type else None,
                model=str(model) if isinstance(model, str) else None,
                enabled=bool(provider.get("enable", False)),
            )
        )
    return AdminBotProviders(configured=True, available=True, items=items)


@router.patch("/providers/{provider_id}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_bot_provider_toggle(
    provider_id: str,
    payload: AdminBotEnabledPatch,
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """启用/停用一个模型提供商（代理 bot，成功后写审计）。

    Args:
        provider_id: 提供商 id。
        payload: 目标启用状态。
        request: 入站请求，用于审计来源 IP。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        bot 返回的 data。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 不可达（503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")
    try:
        async with _bot_client() as client:
            resp = await client.patch(
                f"/providers/{quote(provider_id, safe='')}/enabled",
                json={"enabled": payload.enabled},
            )
            resp.raise_for_status()
            envelope = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot provider toggle failed: %s", exc)
        raise HTTPException(status_code=503, detail="bot panel is unavailable") from exc
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        raise BizError(CommonErr.UNAVAILABLE, "bot panel rejected the update")
    await record_audit(
        db,
        cur.id,
        _ACTION_PROVIDER_TOGGLE,
        detail=f"provider={provider_id} enabled={payload.enabled}",
        ip_address=client_ip(request),
    )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


_ACTION_PLUGIN_TOGGLE = "admin_bot_plugin_toggle"


@router.get("/plugins", response_model=ApiResp[AdminBotList])
@respond
async def admin_bot_plugins(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotList:
    """插件列表（名称/版本/启用状态）。

    Args:
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        插件列表；未配置或不可达时降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotList(configured=False, available=False, reason="not_configured")
    try:
        async with _bot_client() as client:
            data = await _bot_get(client, "/plugins")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot plugins unavailable: %s", exc)
        return AdminBotList(configured=True, available=False, reason="unavailable")

    raw_items = data if isinstance(data, list) else None
    items = []
    for plugin in raw_items or []:
        if not isinstance(plugin, dict):
            continue
        name = str(plugin.get("name") or plugin.get("id") or "")
        if not name:
            continue
        items.append(
            AdminBotListItem(
                id=name,
                name=name,
                subtitle=str(plugin.get("version") or "") or None,
                enabled=bool(plugin.get("activated", plugin.get("enabled", False))),
            )
        )
    return AdminBotList(configured=True, available=True, items=items)


@router.patch("/plugins/{plugin_name}", response_model=ApiResp[dict[str, Any]])
@respond
async def admin_bot_plugin_toggle(
    plugin_name: str,
    payload: AdminBotEnabledPatch,
    request: Request,
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """启用/停用一个插件（代理 bot，成功后写审计）。

    Args:
        plugin_name: 插件目录名。
        payload: 目标启用状态。
        request: 入站请求，用于审计来源 IP。
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        bot 返回的 data。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
        HTTPException: 未配置或 bot 不可达（503）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        raise HTTPException(status_code=503, detail="bot panel is not configured")
    try:
        async with _bot_client() as client:
            resp = await client.patch(
                f"/plugins/{quote(plugin_name, safe='')}/enabled",
                json={"enabled": payload.enabled},
            )
            resp.raise_for_status()
            envelope = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot plugin toggle failed: %s", exc)
        raise HTTPException(status_code=503, detail="bot panel is unavailable") from exc
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        raise BizError(CommonErr.UNAVAILABLE, "bot panel rejected the update")
    await record_audit(
        db,
        cur.id,
        _ACTION_PLUGIN_TOGGLE,
        detail=f"plugin={plugin_name} enabled={payload.enabled}",
        ip_address=client_ip(request),
    )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


@router.get("/knowledge", response_model=ApiResp[AdminBotList])
@respond
async def admin_bot_knowledge(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotList:
    """知识库列表（id/名称/文档数）。

    Args:
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        知识库列表；未配置或不可达时降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotList(configured=False, available=False, reason="not_configured")
    try:
        async with _bot_client() as client:
            data = await _bot_get(client, "/knowledge-bases")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot knowledge unavailable: %s", exc)
        return AdminBotList(configured=True, available=False, reason="unavailable")

    raw_items = data.get("items") if isinstance(data, dict) else None
    items = []
    for kb in raw_items or []:
        if not isinstance(kb, dict):
            continue
        kb_id = str(kb.get("kb_id") or kb.get("id") or "")
        if not kb_id:
            continue
        doc_count = kb.get("doc_count")
        items.append(
            AdminBotListItem(
                id=kb_id,
                name=str(kb.get("kb_name") or kb.get("name") or kb_id),
                subtitle=None if doc_count is None else f"{doc_count} docs",
                enabled=None,
            )
        )
    return AdminBotList(configured=True, available=True, items=items)


@router.get("/config", response_model=ApiResp[AdminBotList])
@respond
async def admin_bot_config(
    cur: CurrentUser = require_admin,
    db: AsyncSession = Depends(get_session),
) -> AdminBotList:
    """配置档列表（编辑仍走完整面板，2FA step-up 行为不变）。

    Args:
        cur: 当前后台管理员。
        db: 业务库会话，用于判定能力点。

    Returns:
        配置档列表；未配置或不可达时降级。

    Raises:
        BizError: 缺少 ``admin.bot_panel``（403）。
    """
    await require_permission(db, cur, Permission.admin_bot_panel)
    if not _bot_configured():
        return AdminBotList(configured=False, available=False, reason="not_configured")
    try:
        async with _bot_client() as client:
            data = await _bot_get(client, "/config-profiles")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("admin bot config unavailable: %s", exc)
        return AdminBotList(configured=True, available=False, reason="unavailable")

    raw_items = data.get("info_list") if isinstance(data, dict) else None
    items = []
    for profile in raw_items or []:
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("id") or "")
        if not profile_id:
            continue
        items.append(
            AdminBotListItem(
                id=profile_id,
                name=str(profile.get("name") or profile_id),
                subtitle=None,
                enabled=None,
            )
        )
    return AdminBotList(configured=True, available=True, items=items)
