"""AUTH 内部 HTTP 读端点（M3 B1.2）：供单体/他进程经 HTTP 跨缝取单用户快照（冻结字段）。

装配：本 router 与业务域名前缀分开、显式带 ``/auth/internal``；**只**承载内部读、不承载任何
浏览器可触的高危面。monolith（经 registry ROUTERS）与独立 AUTH 进程（main_auth 显式挂）都挂
载它 → 两个进程都能 serve 同一读契约；B1.2 只 build 缝 + client + flag + 端点，网关 ``/auth/**``
路由（B1.3）在下个 leg。

鉴权模型（防新公共 blast surface）——**内部共享 token**（与 files/notify 同类最小摩擦力）：
- 要求 ``Authorization: Bearer {settings.auth_http_token}``。token 未配置（默认空）→ 一律 401
  (fail-closed)，此端点不成为公网面；token 错/缺 → 401。
- 只读缝**冻结字段**，**不透出** email/phone/hashed_password 等 PII/凭证（server 侧仅切
  ``snapshot._fetch_fields_from_db``——与本地 A6 同源，字段面零加宽）。response 信封为内部
  契约（非 ApiResp）：``{"data": <冻结字段 dict|null>, "sv": <int|null>}`` —— 与
  ``auth.user_http`` 的解析完全对齐。

即用即作废 DB 直读权威（不绕 cache、不起 side-effect）：本端点永远从 DB 拉最新快照 + 来源版本，
返回给调用方的既是真值也是可作缓存 CAS 的真实 sv。

会话归属：S5 拆库后 users/profiles 在 **auth 独立库**，故本端点用 ``get_auth_session``（auth 库）
而非业务的 ``get_session``——否则会对业务库不存在的 users 表查询（UndefinedTable）。monolith 与
AUTH 进程都挂本 router，两进程的 ``get_auth_session`` 都指向各自配置的 auth 库。
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.secrets import reveal
from auth import snapshot as snap_mod
from auth.db.session import get_auth_session

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])


def _require_internal_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """内部共享令牌鉴权：未配置/缺/错 都 401（fail-closed，此缝不成为公网面）。"""
    token = reveal(settings.auth_http_token)
    if not token:
        raise HTTPException(status_code=401, detail="internal read not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, token):
        raise HTTPException(status_code=401, detail="bad internal token")


@router.get("/users/{user_id}/snapshot")
async def internal_user_snapshot(
    user_id: uuid.UUID,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """经内部缝按 id 拉单用户快照（冻结字段）+ 来源版本 sv。

    返回 ``{"data": <fields|null>, "sv": <int|null>}``；用户不存在 → ``data=None, sv=None``。
    只读冻结字段、零 PII；端点是 authoritative DB 直读（不绕 cache / 不起 seam side-effect），
    真 sv 给调用方做缓存 CAS。
    """
    fields, version = await snap_mod._fetch_fields_from_db(user_id, db)
    return {"data": fields, "sv": version}


def _parse_ids(ids: str) -> list[uuid.UUID]:
    """解析 ``ids=<uuid>,<uuid>``：去重 + 保序（首次出现序）；非法/空/超限即 400（fail-closed）。

    上限用 ``snapshot.BATCH_IDS_MAX``（与业务侧分块同一常量）——超限直接拒，不静默截断：
    截断会让调用方以为全部取到，属静默错答案。去重避免同 id 重复占额度与重复行。
    """
    raw = [p.strip() for p in ids.split(",")]
    parsed: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for p in raw:
        if not p:
            continue
        try:
            uid = uuid.UUID(p)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad user id: {p!r}") from None
        if uid not in seen:
            seen.add(uid)
            parsed.append(uid)
    if not parsed:
        raise HTTPException(status_code=400, detail="ids is empty")
    if len(parsed) > snap_mod.BATCH_IDS_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"too many ids: {len(parsed)} > {snap_mod.BATCH_IDS_MAX}",
        )
    return parsed


@router.get("/users/by-ids")
async def internal_users_by_ids(
    ids: str = Query(description="逗号分隔的用户 id，≤BATCH_IDS_MAX 个（自动去重）"),
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """经内部缝**一次**拉一批用户快照（M6.5，消跨 AUTH 逐 id HTTP 循环）。

    返回 ``{"items": [{"user_id": <int>, "data": <fields|null>, "sv": <int|null>}, ...]}``
    ——每个**入参 id** 都有一条（权威不存在 → ``data=null``），顺序与去重后的入参一致。只读
    冻结字段、零 PII，与单条端点同源（``_fetch_fields_batch_from_db``：一条 SQL 查多行）。
    """
    parsed = _parse_ids(ids)
    rows = await snap_mod._fetch_fields_batch_from_db(parsed, db)
    return {
        "items": [
            {
                "user_id": uid,
                "data": rows[uid][0] if uid in rows else None,
                "sv": rows[uid][1] if uid in rows else None,
            }
            for uid in parsed
        ]
    }
