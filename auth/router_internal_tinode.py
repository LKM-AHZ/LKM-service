"""Tinode REST 认证桥：LKM access token ⇄ Tinode 账号。

Tinode 以 ``use_separate_endpoints=true`` 逐个 POST 到
``/auth/internal/tinode/<op>``（配置见 ``deploy/tinode/tinode.conf``）。四个回调里只有
``auth`` 与 ``link`` 需要实现：

- ``auth``：客户端 ``login {scheme:"basic", secret: base64("lkm:" + access_token)}``。
  验签/撤销/账号状态判据直接复用 auth 进程的 ``_resolve_current_user``（与业务进程同源）。
  尚无映射时返回 ``newacc`` 让 Tinode 建号；建号后 Tinode 回调 ``link`` 把分配到的 uid
  报回来，落在 ``tinode_links``。此后 ``auth`` 直接返回该 uid。
- ``link``：写映射（幂等；uid 已在 Tinode 侧确定）。
- ``gen`` / ``add`` / ``checkunique``：账号真身由 Tinode 管理，一律 ``unsupported``。
- ``rtagns``：Tinode 启动时同步受限标签命名空间；LKM 不限制，回空列表。

安全：网关已 403 拦截 ``/api/v1/auth/internal/``；本模块再以 query token 做第二道
fail-closed 校验（未配置 → 401），端点只应经容器网络被 Tinode 调用。
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import get_auth_session
from auth.deps import _resolve_current_user
from auth.models import Profile, TinodeLink, User
from core.config import settings
from core.err import BizError, CommonErr
from core.secrets import reveal

router = APIRouter(prefix="/auth/internal/tinode", tags=["auth-internal"])

_LOGIN_PREFIX = "lkm:"
# Tinode 0.25 的 ``link`` 回调给的是裸 uid（base64url，实测 12 字符），登录响应的
# ``user`` 才是 ``usr`` + 裸 uid。两版都见过，桥原样存取、不增删前缀，只校验形状。
_UID_PATTERN = re.compile(r"^(?:usr)?[A-Za-z0-9_-]{10,30}$")
# Tinode 默认访问模式（官方 REST auth 示例值）：登录用户可 join/read/write/presence，
# 匿名只有 none。具体房间权限由 Tinode 房间自身 ACL 决定，这里只是账号默认值。
_DEFAULT_AUTH_ACS = "JRWPS"
_DEFAULT_ANON_ACS = "N"


class _Record(BaseModel):
    """Tinode 回传的认证记录；桥只关心 uid。"""

    uid: str | None = None


class _BridgeIn(BaseModel):
    """Tinode REST auth 的请求体（字段见 server/auth/rest/README.md）。

    未列出的字段（``addr`` / ``byteval`` 等）由 Pydantic 默认忽略。
    """

    endpoint: str | None = None
    secret: str | None = None
    rec: _Record | None = None


class _BridgeRejected(Exception):
    """凭据无效：按 Tinode REST auth 契约回 ``{"err":"credentials"}``（HTTP 200）。"""


def _require_bridge_token(token: str | None = Query(default=None)) -> None:
    """Tinode 侧共享令牌：未配置/缺/错都 401（fail-closed）。"""
    expected = reveal(settings.tinode_auth_token)
    if not expected:
        raise BizError(CommonErr.UNAUTHORIZED, "tinode bridge not configured")
    if not token or not secrets.compare_digest(token.encode(), expected.encode()):
        raise BizError(CommonErr.UNAUTHORIZED, "bad tinode bridge token")


def _decode_secret(secret: str | None) -> str | None:
    """把 ``base64("lkm:<access token>")`` 解成裸 access token。"""
    if not secret:
        return None
    try:
        raw = base64.b64decode(secret, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    if not raw.startswith(_LOGIN_PREFIX):
        return None
    token = raw[len(_LOGIN_PREFIX) :]
    return token or None


def _rec(user_id: uuid.UUID, uid: str | None) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "authlvl": "auth",
        "state": "ok",
        "tags": [f"lkm:{user_id}"],
    }
    if uid:
        rec["uid"] = uid
    return rec


async def _resolve_lkm_user(db: AsyncSession, secret: str | None) -> User:
    """验 LKM access token 并取用户行；任何失败都转成 ``_BridgeRejected``。"""
    token = _decode_secret(secret)
    if token is None:
        raise _BridgeRejected
    try:
        current = await _resolve_current_user(token, db)
    except BizError as exc:
        raise _BridgeRejected from exc
    user = await db.get(User, current.id)
    if user is None:
        raise _BridgeRejected
    return user


async def _link_for(db: AsyncSession, user_id: uuid.UUID) -> TinodeLink | None:
    return await db.get(TinodeLink, user_id)


async def _display_name(db: AsyncSession, user: User) -> str:
    profile = await db.get(Profile, user.id)
    return (profile.nickname if profile else None) or user.username


@router.post("/auth")
async def tinode_auth(
    body: _BridgeIn,
    _bridge: None = Depends(_require_bridge_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """登录：有映射回 uid；无映射回 ``newacc`` 让 Tinode 建号并回调 ``link``。"""
    try:
        user = await _resolve_lkm_user(db, body.secret)
    except _BridgeRejected:
        return {"err": "credentials"}

    link = await _link_for(db, user.id)
    if link is not None:
        return {"rec": _rec(user.id, link.tinode_uid)}

    return {
        "rec": _rec(user.id, None),
        "newacc": {
            "auth": _DEFAULT_AUTH_ACS,
            "anon": _DEFAULT_ANON_ACS,
            "public": {"fn": await _display_name(db, user)},
            "trusted": {},
            "private": {},
        },
    }


@router.post("/link")
async def tinode_link(
    body: _BridgeIn,
    _bridge: None = Depends(_require_bridge_token),
    db: AsyncSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """建号回调：把 Tinode 分配的 uid 写进映射表（幂等）。"""
    uid = (body.rec.uid if body.rec else None) or ""
    if not _UID_PATTERN.fullmatch(uid):
        return {"err": "malformed"}
    try:
        user = await _resolve_lkm_user(db, body.secret)
    except _BridgeRejected:
        return {"err": "credentials"}

    link = await _link_for(db, user.id)
    if link is None:
        db.add(TinodeLink(user_id=user.id, tinode_uid=uid))
    elif link.tinode_uid != uid:
        link.tinode_uid = uid
    try:
        await db.commit()
    except IntegrityError:
        # 同一用户并发首登：另一请求已写入不同 uid。回 err 让 Tinode 回滚刚建的账号，
        # 而不是留下孤儿号——客户端重试即可拿到既有映射。
        await db.rollback()
        return {"err": "failed"}
    return {"rec": _rec(user.id, uid)}


@router.post("/gen")
async def tinode_gen(
    _body: _BridgeIn, _bridge: None = Depends(_require_bridge_token)
) -> dict[str, Any]:
    """Tinode 管理账号凭据，外部源不签发。"""
    return {"err": "unsupported"}


@router.post("/add")
async def tinode_add(
    _body: _BridgeIn, _bridge: None = Depends(_require_bridge_token)
) -> dict[str, Any]:
    """账号由 Tinode 建（见 ``auth`` 的 ``newacc``），外部源不重复建号。"""
    return {"err": "unsupported"}


@router.post("/checkunique")
async def tinode_check_unique(
    _body: _BridgeIn, _bridge: None = Depends(_require_bridge_token)
) -> dict[str, Any]:
    return {"err": "unsupported"}


@router.post("/rtagns")
async def tinode_restricted_tag_namespaces(
    _body: _BridgeIn, _bridge: None = Depends(_require_bridge_token)
) -> dict[str, Any]:
    """受限标签命名空间：Tinode 启动时同步一次。

    LKM 不限制标签命名空间（账号 tags 只用于回传 `lkm:<uuid>`），返回空列表。
    缺此端点会让 Tinode 启动时等待 30s 超时才继续（实测）。
    """
    return {"strarr": []}
