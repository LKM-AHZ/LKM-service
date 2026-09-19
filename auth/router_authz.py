"""AUTH 内部授权/升权/验密 写缝（M3.B S2）：供业务进程经 HTTP 把“升权/升格/凭证校验”交给 auth 权威。

定位：auth 拆独立库后，业务进程不会再本地动 users/profiles；一切**身份状态的变更**（解锁考试、
纳入成员）与**凭证校验**（blog git_http 等需要验密）都收口到 auth。本 router 只挂 ``/auth/internal``，
与 router_read 共用同一内部 Bearer 共享令牌（未配置即 fail-closed 401，不成公网面），并操作 auth 侧
自持库会话——S5 拆库后统一走 ``get_auth_session``（auth 库），不再用业务 ``get_session``。

装配：并入 auth 域 ROUTERS（auth 进程与 monolith 都挂载本 router——同一进程内直接走实现亦无碍）。
跨库语义在 Phase 4 接线：业务 → 内部 client → 打到 auth 内部端点，由 auth 进程落库并发 user 事件失效。

写缝语义（Phase 4 消费）：升权只单向提升不降级、有改才 bump token_version；auth 自己的事务内 commit。
返回内部信封（非 ApiResp）：grant → ``{"changed": 0|1}``；verify-password → ``{"ok": bool}``。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import get_auth_session
from auth.models import User
from auth.router_read import _require_internal_token
from auth.security import verifypwd
from auth.service_authz import (
    authorize_user,
    grant_exam_unlock,
    grant_incubation,
)

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])


class _AuthzIn(BaseModel):
    user_id: uuid.UUID
    # 会话描述：monolith 已在其侧自行解码 JWT(用共享 jwt_secret)，把“需 auth 侧复核/裁决”的关键
    # 载荷原样送来复审；不带 email/phone→ 缝不透 PII。
    expect_token_version: int = 0
    iat_ts: int | float | None = None  # JWT iat(秒)；None=不检查改密撤销
    require_admin: bool = False  # 后台：要求 account_level == admin


class _GrantIn(BaseModel):
    kind: Literal["exam_unlock", "incubation"]
    user_id: uuid.UUID
    # 仅 kind=exam_unlock 用到：考试解锁目标 level/role（可空，空则该侧不升）
    unlock_level: str | None = None
    unlock_role: str | None = None


class _VerifyPasswordIn(BaseModel):
    username: str
    password: str


@router.post("/authz")
async def internal_authz(
    body: _AuthzIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, object]:
    """auth 权威裁决：会话是否存活 + 返回当前 account_level/role。返回内部信封：

    ``{"ok": bool, "cause": str|null, "account_level": str|null, "role": str|null}``。
    消费方（monolith deps seam）据 ok/cause 抛对应 BizError 并重建 CurrentUser。
    """
    return await authorize_user(
        db,
        user_id=body.user_id,
        expect_token_version=body.expect_token_version,
        iat_ts=body.iat_ts,
        require_admin=body.require_admin,
    )


@router.post("/grant")
async def internal_grant(
    body: _GrantIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, int]:
    """按 kind 执行 auth 侧单向升权（解锁考试 / 纳入成员）。返回 ``{"changed": 0|1}``。

    auth 官方写面委托点：grant_* 原语在 get_session 注入的事务内执行并随之 commit；真实升权会
    bump token_version 并入队 user.updated —— 消费侧随后拉到的快照为已升权新值、旧令牌已失效。
    """
    if body.kind == "incubation":
        changed = await grant_incubation(db, body.user_id)
    else:  # kind == "exam_unlock"
        changed = await grant_exam_unlock(
            db,
            body.user_id,
            unlock_level=body.unlock_level,
            unlock_role=body.unlock_role,
        )
    return {"changed": changed}


@router.post("/verify-password")
async def internal_verify_password(
    body: _VerifyPasswordIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, bool]:
    """校验用户名+密码（凭证例外路径，如 blog/git_http）。返回 ``{"ok": bool}``。

    只读校验：不锁用户、不产生审计。仅当用户存在且凭据匹配返回 true。
    """
    user = (
        (await db.execute(select(User).where(User.username == body.username)))
        .scalars()
        .first()
    )
    ok = bool(user and user.hashed_password)
    if ok:
        try:
            ok = await verifypwd(body.password, str(user.hashed_password))  # type: ignore[union-attr]
        except Exception:
            ok = False
    return {"ok": ok}
