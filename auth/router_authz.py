"""AUTH 内部授权/升权/验密 写缝（M3.B S2）：供业务进程经 HTTP 把“升权/升格/凭证校验”交给 auth 权威。

定位：auth 拆独立库后，业务进程不会再本地动 users/profiles；一切**身份状态的变更**（解锁考试、
纳入成员）与**凭证校验**（blog git_http 等需要验密）都收口到 auth。本 router 只挂 ``/auth/internal``，
与 router_read 共用同一内部 Bearer 共享令牌（未配置即 fail-closed 401，不成公网面），并操作 auth 侧
自持库会话——S5 拆库后统一走 ``get_auth_session``（auth 库），不再用业务 ``get_session``。

装配：并入 auth 域 ROUTERS（auth 进程与 monolith 都挂载本 router——同一进程内直接走实现亦无碍）。
跨库语义在 Phase 4 接线：业务 → 内部 client → 打到 auth 内部端点，由 auth 进程落库并发 user 事件失效。

写缝语义（Phase 4 消费）：升权只单向提升不降级、有改才 bump token_version；auth 自己的事务内 commit。
返回内部信封（非 ApiResp）：grant → ``{"changed": 0|1}``；
verify-password → ``{"ok": bool, "user_id": str|null, "username": str|null}``。
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import get_auth_session
from auth.models import User
from auth.router_read import _require_internal_token
from auth.security import dummy_verify, verifypwd
from auth.service_authz import (
    authorize_user,
    grant_exam_unlock,
    grant_incubation,
)

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])

logger = logging.getLogger(__name__)


class _AuthzIn(BaseModel):
    user_id: uuid.UUID
    # 会话描述：monolith 已在其侧自行解码 JWT(用共享 jwt_secret)，把“需 auth 侧复核/裁决”的关键
    # 载荷原样送来复审；不带 email/phone→ 缝不透 PII。
    # 两个字段**故意不给默认值**：原先 expect_token_version 默认 0（与绝大多数账号的初始
    # token_version 相同）、iat_ts 默认 None（跳过改密撤销），调用方漏传就把权威裁决静默降级成
    # 「存在且未锁定」。改为必填，漏传直接 422。
    expect_token_version: int
    iat_ts: int | float | None  # JWT iat(秒)；显式传 None 才跳过改密撤销检查
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
        # incubation 只按 user_id 升权：带了 unlock_* 说明调用方拼错了 payload，
        # 静默丢弃会让它拿到「成功」的 changed 却什么都没升
        if body.unlock_level is not None or body.unlock_role is not None:
            raise HTTPException(
                status_code=422,
                detail="unlock_level/unlock_role not allowed for incubation",
            )
        changed = await grant_incubation(db, body.user_id)
    else:  # kind == "exam_unlock"
        # 两个目标都没给 → grant_exam_unlock 只会返回 0，与「本来就已经解锁」无法区分；
        # 业务侧 _apply_unlock 已保证不会这么发，故这里显式拒绝而不是静默 no-op
        if body.unlock_level is None and body.unlock_role is None:
            raise HTTPException(
                status_code=422,
                detail="exam_unlock requires unlock_level and/or unlock_role",
            )
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
) -> dict[str, object]:
    """校验用户名+密码（凭证例外路径，如 blog/git_http）。返回 ``{"ok", "user_id", "username"}``。

    只读校验：不锁用户、不产生审计。仅当用户存在且凭据匹配返回 true，并一并回身份
    ``user_id``/``username``——调用方（blog git push 的属主判定）需拿 id 与 ``series.owner_id``
    比对，而身份读缝 ``auth.snapshot`` 刻意不含凭证列、走不了那条路。``ok=false`` 时两者为 null。
    """
    user = (
        (await db.execute(select(User).where(User.username == body.username)))
        .scalars()
        .first()
    )
    if not (user and user.hashed_password):
        # 用户不存在/无密码：跑一次等成本的虚拟哈希，否则本条“微秒返回、存在者数十毫秒”
        # 的耗时差可被调用方（如 blog git_http）当作用户名枚举 oracle。
        await dummy_verify()
        return {"ok": False, "user_id": None, "username": None}
    ok = True
    try:
        ok = await verifypwd(body.password, str(user.hashed_password))
    except Exception:
        # 与 service_auth.authenticate_account 同口径：凭证哈希损坏/依赖异常不能与「密码错」
        # 混为一谈（否则根因丢失，只剩一个笼统的 ok=false）
        logger.exception(
            "verifypwd raised exception for user_id=%s (possible corrupted hash)", user.id
        )
        ok = False
    if not ok:
        return {"ok": False, "user_id": None, "username": None}
    return {"ok": True, "user_id": str(user.id), "username": user.username}
