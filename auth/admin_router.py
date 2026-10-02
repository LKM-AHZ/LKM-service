"""后台 cookie 会话写面 —— AUTH 域自足版（S5-A2 Step0，additive）。

admin 会话真值收进 auth 域后，本 router 承载后台 4 个**写面**端点（登录/刷新/登出/
2FA step-up），DB 走 独立 auth 库（``auth.db.session.get_auth_session``），把 auth 域
已迁表的 ``users/profiles/refresh_tokens/totp`` 作为真值（去单独体 biz 库的 users）。

纯新增、不改单体现成文件（admin domain 的 auth_router.py 仍留着直至 Step1 才摘）。

*owner-leaf 合规*：本模块**只 import auth 域内部 + core + core.db.base/err/session**，
绝不 import 业务域（admin/rbac/board...）。签名/校验所用后台 cookie 基元（COOKIE_NAME、
COOKIE_PATH、_ADMIN_AUD、create_admin_access_token 等）取自
``auth.admin_session``（auth 域单一事实源），不复制第二份。

行为语义与 URL 均对齐单体现行 ``app/modules/admin/auth_router.py``（该文件 admin 4 写面
的迁移）：prefix ``/admin/auth``（挂上 AUTH 进程 api_prefix 后 URL 形如 ``/api/v1/admin/
auth/login``），前后台分离的 cookie 名 + audience 不变。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import jwt_keys
from auth.admin_session import (
    _ADMIN_AUD,
    COOKIE_NAME,
    COOKIE_PATH,
    MFA_TRUST_SECONDS,
    REFRESH_NAME,
    create_admin_access_token,
)
from auth.db.session import get_auth_session
from auth.errors import AuthErr
from auth.models import RefreshToken, User
from auth.repository import RevokedAccessTokenRepository, UserRoleRepository
from auth.security import PASSWORD_MAX_LENGTH, dummy_verify, verifypwd
from auth.service_2fa import verify_user_totp
from auth.service_auth import (
    generate_refresh_token,
    hash_refresh_token,
    valid_refresh_token,
)
from auth.service_roles import list_user_roles, list_users_for_role, set_user_role
from auth.service_verify import check_code_rate_limit
from auth.token_revocation import block_payload_jti, is_jti_blocked
from core.client_ip import client_ip
from core.config import settings
from core.db.base import now_iso
from core.db.repo import consume_once, get_or_raise
from core.err import BizError, CommonErr, resp_json
from core.rbac_roles import (
    SSD_CONSTRAINTS,
    activated_roles,
    authorized_roles,
    role_closure,
    satisfies_constraints,
    session_roles_claim,
)

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


async def _require_role_manager(request: Request, db: AsyncSession) -> User:
    actor = await _require_admin_from_cookie(request, db)
    if not _current_mfa_trust(request)[0]:
        raise BizError(CommonErr.MFA_REQUIRED)
    if "admin:super_admin" not in await _active_cookie_roles(request, db, actor):
        raise BizError(CommonErr.FORBIDDEN)
    return actor


async def _active_cookie_roles(
    request: Request, db: AsyncSession, user: User
) -> tuple[str, ...]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise BizError(CommonErr.FORBIDDEN)
    try:
        payload = jwt_keys.decode(token, audience=_ADMIN_AUD)
    except jwt.InvalidTokenError as exc:
        raise BizError(CommonErr.FORBIDDEN, "Session invalid") from exc
    await db.refresh(user, attribute_names=["profile"])
    role = user.profile.role if user.profile else "member"
    try:
        selection = session_roles_claim(payload.get("active_roles"))
        assigned = await UserRoleRepository(db).list_roles(user.id)
        if not satisfies_constraints(
            authorized_roles(str(user.account_level), role, assigned), SSD_CONSTRAINTS
        ):
            raise ValueError("Assigned roles violate SSD")
        return activated_roles(
            str(user.account_level),
            role,
            assigned,
            selection,
        )
    except ValueError as exc:
        raise BizError(CommonErr.FORBIDDEN, "Session roles invalid") from exc


class _AdminActivateRolesRequest(BaseModel):
    roles: list[str]


@router.post("/roles/activate")
async def activate_admin_roles(
    body: _AdminActivateRolesRequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    user = await _require_admin_from_cookie(request, db)
    raw_refresh = request.cookies.get(REFRESH_NAME)
    if not raw_refresh or not valid_refresh_token(raw_refresh):
        raise BizError(CommonErr.FORBIDDEN, "Refresh session missing")
    old = jwt_keys.decode(request.cookies[COOKIE_NAME], audience=_ADMIN_AUD)
    refresh_hash = hash_refresh_token(raw_refresh)
    if old.get("rt_hash") != refresh_hash:
        raise BizError(CommonErr.FORBIDDEN, "Access and refresh sessions differ")
    stored = await db.scalar(
        select(RefreshToken)
        .where(
            RefreshToken.token_hash == refresh_hash,
            RefreshToken.user_id == user.id,
            RefreshToken.kind == "admin",
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now_iso(),
        )
        .with_for_update()
    )
    if stored is None:
        raise BizError(CommonErr.FORBIDDEN, "Refresh session invalid")
    await db.refresh(user, attribute_names=["profile"])
    role = user.profile.role if user.profile else "member"
    try:
        roles = activated_roles(
            str(user.account_level),
            role,
            await UserRoleRepository(db).list_roles(user.id),
            body.roles,
        )
    except ValueError as exc:
        raise BizError(CommonErr.INVALID_INPUT, str(exc)) from exc
    stored.active_roles = list(roles)
    access = create_admin_access_token(
        user,
        mfa_verified=bool(old.get("mfa")),
        mfa_at=old.get("mfa_at") if isinstance(old.get("mfa_at"), int) else None,
        active_roles=roles,
        session_expires_at=stored.expires_at,
        refresh_token_hash=refresh_hash,
    )
    resp = resp_json(CommonErr.OK, data={"active_roles": roles})
    _set_access_cookie(resp, access, max_age=_remaining_access_age(stored.expires_at))
    return resp


@router.get("/users/{user_id}/roles")
async def read_user_roles(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, list[str]]:
    await _require_role_manager(request, db)
    direct = await list_user_roles(db, user_id)
    return {"roles": list(direct), "authorized_roles": list(role_closure(direct))}


@router.get("/roles/{role_name}/users")
async def read_role_users(
    role_name: str,
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, list[uuid.UUID]]:
    await _require_role_manager(request, db)
    return {
        "user_ids": await list_users_for_role(db, role_name, limit=limit, offset=offset)
    }


@router.put("/users/{user_id}/roles/{role_name}")
async def assign_user_role(
    user_id: uuid.UUID,
    role_name: str,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, bool]:
    await _require_role_manager(request, db)
    return {"changed": await set_user_role(db, user_id, role_name, assigned=True)}


@router.delete("/users/{user_id}/roles/{role_name}")
async def revoke_user_role(
    user_id: uuid.UUID,
    role_name: str,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, bool]:
    await _require_role_manager(request, db)
    return {"changed": await set_user_role(db, user_id, role_name, assigned=False)}


class _AdminLoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    # 旧账号可能使用短密码；登录只限制计算成本，新密码策略由注册/重置控制。
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)


class _AdminVerify2FARequest(BaseModel):
    code: str = Field(..., min_length=1)


def _current_mfa_trust(request: Request) -> tuple[bool, int | None]:
    """解析当前 access cookie 的 2FA 信任状态，供 refresh 继承（避免信任被 15min cookie 过期截断）。

    语义与单体现行 admin/auth_router 完全一致；token 缺失/失效/非 admin/过期一律视为未信任。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False, None
    try:
        payload = jwt_keys.decode(token, audience=_ADMIN_AUD)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        return False, None
    if payload.get("type") != "admin" or not payload.get("mfa"):
        return False, None
    mfa_at = payload.get("mfa_at")
    if mfa_at is None:
        return False, None
    try:
        trusted_until = datetime.datetime.fromtimestamp(
            float(mfa_at), tz=datetime.UTC
        ) + datetime.timedelta(seconds=MFA_TRUST_SECONDS)
    except (TypeError, ValueError, OSError, OverflowError):
        # mfa_at 是 JWT claim：非数值/超范围会让 fromtimestamp 抛错并冒成 500；
        # 按「未通过 step-up」处理即可（调用方会要求重新验证 MFA）
        return False, None
    if trusted_until < datetime.datetime.now(datetime.UTC):
        return False, None
    return True, int(mfa_at)


# -- cookie helper（复用 admin_session 常量，行为对齐单体现行）-----------------


def _set_access_cookie(
    resp: Response, token: str, *, max_age: int | None = None
) -> None:
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=max_age
        if max_age is not None
        else settings.admin_access_cookie_minutes * 60,
        path=COOKIE_PATH,
    )


def _remaining_access_age(expires_at: datetime.datetime) -> int:
    return max(
        0,
        min(
            settings.admin_access_cookie_minutes * 60,
            int((expires_at - now_iso()).total_seconds()),
        ),
    )


def _set_refresh_cookie(
    resp: Response, token: str, *, max_age: int | None = None
) -> None:
    resp.set_cookie(
        key=REFRESH_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=max_age
        if max_age is not None
        else settings.refresh_token_expire_days * 86400,
        path=COOKIE_PATH,
    )


def _clear_cookies(resp: Response) -> None:
    resp.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    resp.delete_cookie(REFRESH_NAME, path=COOKIE_PATH)


def _admin_user_dict(user: User) -> dict[str, Any]:
    """后台返回的管理员自身信息（对齐单体现行 AdminUserOut 字段，无 PII 富字段）。"""
    return {
        "id": str(user.id),  # 主键已 UUID；JSON 无 uuid 类型，按字符串出 wire
        "username": str(user.username),
        "account_level": str(user.account_level),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# -- 2FA step-up 需先确认当前会话是合法 admin（复用 admin_session 基元，本地裁决于 auth 库）--


async def _revoke_jti_persistently(db: AsyncSession, payload: dict[str, Any]) -> None:
    """把 jti 落 DB 撤销表（Redis 预检之外的**权威**面）。

    关掉 Redis 持久化后 ``jti:block:`` 重启即空，而 admin 单设备登出刻意不 bump
    ``token_version``，jti 是唯一撤销判据 —— 故必须同时落库，否则重启后已登出的 admin
    access cookie 会在剩余 15min 内复活。

    载荷缺 jti/sub/exp 时静默跳过（与 ``block_payload_jti`` 的宽松处理一致：灰度期无 jti
    的旧 token 视为无需处理，ex 缺失无法界定 TTL 也不落库——预检仍会挡）。
    """
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        return
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (AttributeError, TypeError, ValueError):
        return
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return
    await RevokedAccessTokenRepository(db).revoke(
        jti=jti,
        user_id=user_id,
        expires_at=datetime.datetime.fromtimestamp(float(exp), tz=datetime.UTC),
    )


async def _jti_revoked_in_db(db: AsyncSession, jti: Any) -> bool:
    """DB 撤销表兜底查询（Redis 未命中/不可用时才走到这里）。命中即拒。"""
    if not isinstance(jti, str) or not jti:
        return False
    return await RevokedAccessTokenRepository(db).is_revoked(jti)


async def _require_admin_from_cookie(request: Request, db: AsyncSession) -> User:
    """按 admin access cookie 识别当前登录管理员（auth 库自足复刻 get_current_admin 语义）。

    相比单体现行依赖业务 admin deps 的 seam 裁决，此处**直连 current DBA=false 只在 auth
    进程**读 auth 库：解析 admin_session cookie → 取 user → 强制 account_level=admin +
    锁定 / token_version / updated_at 校验（与前台一致，防绕过安全态）。非法一律 FORBIDDEN。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise BizError(CommonErr.FORBIDDEN, "Not logged into admin panel")

    try:
        payload = jwt_keys.decode(token, audience=_ADMIN_AUD)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        # 过期/伪造/算法不符的 cookie 是常态（陈旧会话），按「非法一律 FORBIDDEN」收口；
        # 不兜住会从端点冒出 PyJWT 异常 → 500（与 _current_mfa_trust 对齐）。
        raise BizError(CommonErr.FORBIDDEN, "Admin session invalid") from None
    if payload.get("type") != "admin":
        raise BizError(CommonErr.FORBIDDEN, "Not an admin session token")
    jti = payload.get("jti")
    if await is_jti_blocked(jti) or await _jti_revoked_in_db(db, jti):
        raise BizError(CommonErr.FORBIDDEN, "Admin session invalid or expired")
    sub = payload.get("sub")
    # sub 是 str(user.id)（UUID 串）；非法/缺失一律 FORBIDDEN，绝不 500。
    # 先 str() 再解析：拒绝把裸 int 当 128-bit UUID 接受。
    try:
        user_id = uuid.UUID(str(sub))
    except (AttributeError, TypeError, ValueError):
        raise BizError(CommonErr.FORBIDDEN, "Admin session subject invalid") from None

    user = await get_or_raise(db, User, AuthErr.USER_NOT_FOUND, User.id == user_id)
    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        raise BizError(CommonErr.FORBIDDEN, "Admin account is locked")
    if int(payload.get("token_version", 0)) != int(user.token_version):
        raise BizError(CommonErr.FORBIDDEN, "Admin session invalidated")
    # 改密撤销：JWT iat >= user.updated_at（允许 5 秒容差），与前台一致
    if user.updated_at:
        token_iat = payload.get("iat")
        if token_iat is not None:
            token_time = datetime.datetime.fromtimestamp(
                float(token_iat), tz=datetime.UTC
            )
            if user.updated_at - token_time > datetime.timedelta(seconds=5):
                raise BizError(
                    CommonErr.FORBIDDEN, "Admin session invalidated – password changed"
                )
    if user.account_level != "admin":
        raise BizError(CommonErr.FORBIDDEN, "Insufficient admin privilege")
    return user


@router.post("/login")
async def admin_login(
    body: _AdminLoginReq,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """管理员密码登录（auth 库真值，httpOnly cookie 会话）。

    频控两把锁（方案 §8.4）：用户名级 5/5min + 真实 IP 级 20/5min；IP 源
    ``core.client_ip``（网关后读 ``X-Real-IP``）。用 ``request.client.host`` 会拿到
    apisix 容器地址，使这把锁退化成全站共享单桶。
    """
    # 用户名桶带来源 IP：只按 username 计的话，任何人轮换 IP 发 5 次错密码就能把
    # **目标管理员**锁 5 分钟（定向 DoS），而 IP 桶 20/5min 补不上这个缺口
    await check_code_rate_limit(
        f"admin:login:user:{client_ip(request)}:{body.username}",
        max_count=5,
        window=300,
    )
    await check_code_rate_limit(
        f"admin:login:ip:{client_ip(request)}", max_count=20, window=300
    )

    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalars().first()

    if not user:
        await dummy_verify()
        return resp_json(CommonErr.FORBIDDEN, detail="用户名或密码错误")
    if not await verifypwd(body.password, user.hashed_password):
        return resp_json(CommonErr.FORBIDDEN, detail="用户名或密码错误")

    if user.account_level != "admin":
        return resp_json(CommonErr.FORBIDDEN, detail="无后台访问权限")

    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        return resp_json(CommonErr.FORBIDDEN, detail="账号已锁定")

    raw_refresh = generate_refresh_token()
    access_token = create_admin_access_token(
        user, refresh_token_hash=hash_refresh_token(raw_refresh)
    )  # 读 id/account_level/token_version（已加载）
    payload = _admin_user_dict(user)  # 读 created_at 等（已加载）

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            kind="admin",
            mfa_verified=False,
            expires_at=now_iso()
            + datetime.timedelta(days=settings.refresh_token_expire_days),
            revoked_at=None,
        )
    )
    await db.commit()

    resp = resp_json(CommonErr.OK, data=payload)
    _set_access_cookie(resp, access_token)
    _set_refresh_cookie(resp, raw_refresh)
    return resp


@router.post("/refresh")
async def admin_refresh(
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """用 refresh cookie 换新 access + 旋转新 refresh（auth 库原子 consume_once 复用检测）。"""
    # 按来源 IP 分桶：全局单桶时任一客户端打满 30/min 就会让所有管理员的刷新被限流，
    # 而 access cookie 只有 15min，刷新被耗尽等价于把别人一起登出
    await check_code_rate_limit(
        f"admin:token:refresh:ip:{client_ip(request)}", max_count=30, window=60
    )

    raw_refresh = request.cookies.get(REFRESH_NAME)
    if not raw_refresh:
        return resp_json(CommonErr.FORBIDDEN, detail="缺少刷新令牌")
    if not valid_refresh_token(raw_refresh):
        return resp_json(CommonErr.FORBIDDEN, detail="刷新令牌无效")

    tok_hash = hash_refresh_token(raw_refresh)
    now = now_iso()
    if not await consume_once(
        db,
        RefreshToken,
        {"revoked_at": now},
        RefreshToken.token_hash == tok_hash,
        RefreshToken.kind == "admin",
        RefreshToken.revoked_at.is_(None),
    ):
        return resp_json(CommonErr.FORBIDDEN, detail="刷新令牌无效")

    stored = await get_or_raise(
        db,
        RefreshToken,
        AuthErr.TOKEN_INVALID,
        RefreshToken.token_hash == tok_hash,
    )
    if stored.expires_at <= now:
        return resp_json(CommonErr.FORBIDDEN, detail="会话已过期")

    user = await get_or_raise(
        db, User, AuthErr.USER_NOT_FOUND, User.id == stored.user_id
    )
    if user.account_level != "admin":
        return resp_json(CommonErr.FORBIDDEN, detail="会话无效")
    if user.is_locked and user.locked_until and user.locked_until > now:
        return resp_json(CommonErr.FORBIDDEN, detail="账号已锁定")

    # 2FA 信任以 auth 库 refresh 行为真值：access cookie 只活 15min，只读它的话
    # cookie 一过期就得到 (False, None)，新 refresh 行被写成 mfa_verified=False/mfa_at=NULL，
    # 1 小时信任窗口被硬截成 15 分钟（与上面那条注释的意图相反）。同时把信任原点写回新行。
    mfa_ok = bool(stored.mfa_verified and stored.mfa_at)
    mfa_at = int(stored.mfa_at.timestamp()) if stored.mfa_at else None
    try:
        selection = session_roles_claim(stored.active_roles)
    except ValueError as exc:
        raise BizError(CommonErr.FORBIDDEN, "Session roles invalid") from exc
    if selection is not None:
        await db.refresh(user, attribute_names=["profile"])
        role = user.profile.role if user.profile else "member"
        try:
            selection = activated_roles(
                "admin",
                role,
                await UserRoleRepository(db).list_roles(user.id),
                selection,
            )
        except ValueError as exc:
            raise BizError(CommonErr.FORBIDDEN, "Session roles invalid") from exc
    new_refresh = generate_refresh_token()
    access_token = create_admin_access_token(
        user,
        mfa_verified=mfa_ok,
        mfa_at=mfa_at,
        active_roles=selection,
        session_expires_at=stored.expires_at,
        refresh_token_hash=hash_refresh_token(new_refresh),
    )
    payload = _admin_user_dict(user)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(new_refresh),
            kind="admin",
            mfa_verified=mfa_ok,
            mfa_at=stored.mfa_at,
            active_roles=list(selection) if selection is not None else None,
            expires_at=stored.expires_at,
            revoked_at=None,
        )
    )
    await db.commit()

    resp = resp_json(CommonErr.OK, data=payload)
    _set_access_cookie(
        resp, access_token, max_age=_remaining_access_age(stored.expires_at)
    )
    _set_refresh_cookie(
        resp,
        new_refresh,
        max_age=max(0, int((stored.expires_at - now).total_seconds())),
    )
    return resp


@router.post("/logout")
async def admin_logout(
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """登出：auth 库撤销对应 admin refresh 并清空 cookie。"""
    raw_refresh = request.cookies.get(REFRESH_NAME)
    if raw_refresh and valid_refresh_token(raw_refresh):
        tok_hash = hash_refresh_token(raw_refresh)
        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == tok_hash,
                RefreshToken.kind == "admin",
            )
        )
        stored = result.scalars().first()
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = now_iso()
            await db.commit()
    # 本枚 access cookie 按 jti 记入黑名单 → **立即**失效，而不必等 15min 自然过期。
    # 这里刻意不 bump token_version：那会连带踢掉该 admin 的其他设备，jti 才是精准的。
    raw_access = request.cookies.get(COOKIE_NAME)
    if raw_access:
        try:
            payload = jwt_keys.decode(raw_access, audience=_ADMIN_AUD)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
            # 已过期/伪造的 cookie 本就无需拉黑（预检之外也过不了校验）
            payload = None
        if payload is not None:
            await block_payload_jti(payload)
            # 同时落 DB 权威：Redis 预检关掉持久化后不跨重启存活（见 _revoke_jti_persistently）
            await _revoke_jti_persistently(db, payload)
    resp = resp_json(CommonErr.OK, data={"ok": True})
    _clear_cookies(resp)
    return resp


@router.post("/2fa")
async def admin_verify_2fa(
    body: _AdminVerify2FARequest,
    request: Request,
    db: AsyncSession = Depends(get_auth_session),
) -> JSONResponse:
    """危险操作 step-up（auth 库）：验证当前 admin 的 TOTP，通过后签带 2FA 信任的新 access cookie。

    信任窗口 1 小时（MFA_TRUST_SECONDS），未通过不更新信任、仅抛 TOTP_CODE_INVALID
    （经 verify_user_totp 内部 BizError）。
    """
    # 识别当前 admin（auth 库裁决）：无效/非 admin → FORBIDDEN
    user = await _require_admin_from_cookie(request, db)
    raw_refresh = request.cookies.get(REFRESH_NAME)
    if not raw_refresh or not valid_refresh_token(raw_refresh):
        raise BizError(CommonErr.FORBIDDEN, "Refresh session missing")
    refresh_hash = hash_refresh_token(raw_refresh)
    old = jwt_keys.decode(request.cookies[COOKIE_NAME], audience=_ADMIN_AUD)
    if old.get("rt_hash") != refresh_hash:
        raise BizError(CommonErr.FORBIDDEN, "Access and refresh sessions differ")
    result = await db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.token_hash == refresh_hash,
            RefreshToken.kind == "admin",
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now_iso(),
        )
        .with_for_update()
    )
    stored_refresh = result.scalars().first()
    if stored_refresh is None:
        raise BizError(CommonErr.FORBIDDEN, "Refresh session invalid")

    await verify_user_totp(db, user.id, body.code)
    mfa_at = int(datetime.datetime.now(datetime.UTC).timestamp())
    active_roles = await _active_cookie_roles(request, db, user)
    access_token = create_admin_access_token(
        user,
        mfa_verified=True,
        mfa_at=mfa_at,
        active_roles=active_roles,
        session_expires_at=stored_refresh.expires_at,
        refresh_token_hash=refresh_hash,
    )
    payload = _admin_user_dict(user)

    # 同步更新当前会话 refresh 记录的 mfa 状态。
    stored_refresh.mfa_verified = True
    stored_refresh.mfa_at = datetime.datetime.fromtimestamp(mfa_at, tz=datetime.UTC)
    stored_refresh.active_roles = list(active_roles)
    await db.commit()

    resp = resp_json(
        CommonErr.OK, data={**payload, "mfa_verified": True, "mfa_at": mfa_at}
    )
    _set_access_cookie(
        resp, access_token, max_age=_remaining_access_age(stored_refresh.expires_at)
    )
    return resp
