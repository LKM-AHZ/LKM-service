"""AUTH 内部授权/升权/验密/审计 写缝（M3.B S2）：供业务进程经 HTTP 把“升权/升格/凭证校验/审计”交给 auth 权威。

定位：auth 拆独立库后，业务进程不会再本地动 users/profiles；一切**身份状态的变更**（解锁考试、
纳入成员）与**凭证校验**（blog git_http 等需要验密）都收口到 auth。本 router 只挂 ``/auth/internal``，
与 router_read 共用同一内部 Bearer 共享令牌（未配置即 fail-closed 401，不成公网面），并操作 auth 侧
自持库会话——S5 拆库后统一走 ``get_auth_session``（auth 库），不再用业务 ``get_session``。

装配：并入 auth 域 ROUTERS（auth 进程与 monolith 都挂载本 router——同一进程内直接走实现亦无碍）。
跨库语义在 Phase 4 接线：业务 → 内部 client → 打到 auth 内部端点，由 auth 进程落库并发 user 事件失效。

写缝语义（Phase 4 消费）：升权只单向提升不降级、有改才 bump token_version；auth 自己的事务内 commit。
返回内部信封（非 ApiResp）：grant → ``{"changed": 0|1}``；
verify-password → ``{"ok": bool, "user_id": str|null, "username": str|null}``；
audit → ``{"ok": true}``。

审计为什么也在这里：``audit_logs`` 属 ``AuthBase``（auth 独立库），业务会话写它必然
``UndefinedTable``。业务域的审核/删除审计只能经本缝落到 auth 权威库，不能像旧
``app/modules/admin/content_router.py`` 那样把业务 ``get_session`` 塞给 ``log_audit``。
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import get_auth_session
from auth.models import User
from auth.router_read import _require_internal_token
from auth.security import dummy_verify, verifypwd
from auth.service_admission import decide_admission, list_admission_users
from auth.service_auth import log_audit
from auth.service_authz import (
    authorize_user,
    grant_exam_unlock,
    grant_incubation,
)
from core.err import BizError, CommonErr

router = APIRouter(prefix="/auth/internal", tags=["auth-internal"])

logger = logging.getLogger(__name__)


class _AuthzIn(BaseModel):
    user_id: uuid.UUID
    expect_token_version: int
    iat_ts: int | float | None  # JWT iat(秒)；显式传 None 才跳过改密撤销检查
    require_admin: bool = False  # 后台：要求 account_level == admin
    # 单枚 token 的 jti（admin 单设备登出用）。可选：缺省不做 jti 撤销判定——前台/旧调用方
    # 仍有 token_version 兜底；后台路径（app/modules/admin/deps）必传。
    jti: str | None = None
    selected_roles: tuple[str, ...] | None = None


class _GrantIn(BaseModel):
    kind: Literal["exam_unlock", "incubation"]
    user_id: uuid.UUID
    # 仅 kind=exam_unlock 用到：考试解锁目标 level/role（可空，空则该侧不升）
    unlock_level: str | None = None
    unlock_role: str | None = None


class _VerifyPasswordIn(BaseModel):
    username: str
    password: str


class _AuditIn(BaseModel):
    """内部审计写缝入参。

    ``user_id`` 的语义由调用方定义（业务审核决定里用**操作者**，管理员删内容沿用
    “被删内容作者 + detail 记 admin”）。这里不做语义裁决，只保证落库字段合法。
    """

    user_id: uuid.UUID | None = None
    action: str = Field(min_length=1, max_length=200)
    detail: str | None = None
    ip_address: str | None = Field(default=None, max_length=45)


@router.post("/authz")
async def internal_authz(
    body: _AuthzIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, object]:
    """auth 权威裁决：会话是否存活 + 返回当前等级和激活角色。返回内部信封：

    ``{"ok": bool, "cause": str|null, "account_level": str|null,
    "role": str|null, "active_roles": list[str]}``。
    消费方（monolith deps seam）据 ok/cause 抛对应 BizError 并重建 CurrentUser。
    """
    return await authorize_user(
        db,
        user_id=body.user_id,
        expect_token_version=body.expect_token_version,
        iat_ts=body.iat_ts,
        require_admin=body.require_admin,
        jti=body.jti,
        selected_roles=body.selected_roles,
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
            raise BizError(
                CommonErr.INVALID_INPUT,
                "unlock_level/unlock_role not allowed for incubation",
            )
        changed = await grant_incubation(db, body.user_id)
    else:  # kind == "exam_unlock"
        if body.unlock_level is None and body.unlock_role is None:
            raise BizError(
                CommonErr.INVALID_INPUT,
                "exam_unlock requires unlock_level and/or unlock_role",
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
        await dummy_verify()
        return {"ok": False, "user_id": None, "username": None}
    ok = True
    try:
        ok = await verifypwd(body.password, str(user.hashed_password))
    except Exception:
        logger.exception(
            "verifypwd raised exception for user_id=%s (possible corrupted hash)",
            user.id,
        )
        ok = False
    if not ok:
        return {"ok": False, "user_id": None, "username": None}
    return {"ok": True, "user_id": str(user.id), "username": user.username}


@router.get("/admin-ids")
async def internal_admin_ids(
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, list[str]]:
    """列出 ``account_level=admin`` 的用户 id（每日待办摘要的收件人）。

    业务库没有 users 表，管理员名单只能在 auth 权威侧取；返回 id 即可，零 PII。
    按 id 排序保证同一批收件人顺序稳定（通知去重/重放可复现）。
    """
    rows = (
        (
            await db.execute(
                select(User.id)
                .where(User.account_level == "admin")
                .order_by(User.id)
            )
        )
        .scalars()
        .all()
    )
    return {"ids": [str(user_id) for user_id in rows]}


@router.post("/audit")
async def internal_audit(
    body: _AuditIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, bool]:
    """内部审计写缝：业务进程把审核决定/管理员删除等审计落到 auth 权威库。

    为什么需要这条缝：``audit_logs`` 是 ``AuthBase`` 表（auth 独立库），业务进程的
    ``get_session`` 看不到它（拆库后必 ``UndefinedTable``）。业务域只经
    ``core.ports.audit.record_audit`` → 本端点写审计，不直连 auth 库。

    commit 由 ``get_auth_session`` 在请求末完成；**fail-closed**：写失败即 5xx，调用方
    的业务事务必须随之回滚（宁可这次审核/删除不生效，也不留下“做了但没审计”的记录）。

    已知取舍：拆库模式下本端点在业务事务 commit **之前**提交，若业务随后回滚，会留下一条
    “曾尝试”的审计（detail 带目标与决定）。这是跨库无分布式事务的必然结果，方向是**多记**
    而不是漏记；需要严格一致时再上 outbox。
    """
    await log_audit(
        db,
        body.user_id,
        body.action,
        detail=body.detail,
        ip_address=body.ip_address,
    )
    return {"ok": True}


class _AdmissionDecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    reviewer_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=500)


@router.get("/admission")
async def internal_admission_list(
    status: str = Query(default="pending"),
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, list[dict[str, object]]]:
    """按准入状态列用户（pending/rejected/active），供后台审核页读。

    内部缝：含 email，仅供业务进程的服务端调用，不暴露公网。
    """
    return {"items": await list_admission_users(db, status)}


@router.post("/admission/{user_id}")
async def internal_admission_decide(
    user_id: uuid.UUID,
    body: _AdmissionDecisionIn,
    _auth: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, object]:
    """approve/reject 一个待审账号；reject 同时撤销其全部会话。"""
    return await decide_admission(
        db,
        user_id=user_id,
        action=body.action,
        reviewer_id=body.reviewer_id,
        reason=body.reason,
    )
