"""AUTH 内部授权/升权写原语（M3.B S2）：把身份“升权写面”收进 auth 域(在 auth 进程/库内执行)。

背景：exam/service ``_apply_unlock`` 与 projects/service ``_apply_incubation`` 目前在**业务进程**
本地直改 ``User.account_level / Profile.role / User.token_version``——这是拆库后**业务物理不可达
auth 行**而必须外移到 auth 的写面（Phase 4 接线，把业务侧改为经 auth internal 写缝调用本原语）。
本模块把这些“单向升权 + 升格才 token 失效 + 发 user 事件”的权威语义复刻成 auth 内的独立原语，
语义与现存实现**一一对等**（只单向提升、不降级、有改动才 token bump → 发 ``user.updated``）。

不变量（与 service_auth/events 同落位）：升权写面应在 auth 进程的 auth 独立库事务内执行；
真实提升发生时递增 ``token_version`` 使旧令牌失效并按需入队 ``notify_user_updated`` 失效快照
（经 outbox；未配消息总线时门控直返 fail-open）。调用方负责 commit/close。
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import now_iso
from app.modules.auth import events, user_http
from app.modules.auth.models import Profile, User

# account_level / Profile.role 的“单向提升”单调序。auth 是身份词表 owner，故把 exam/service、
# projects/service 各自硬编码的 rank 语义集中到这里（Phase 4 由 auth 侧以此裁决是否真升）。
_LEVEL_RANK = {"local": 0, "normal": 1, "admin": 2}
_ROLE_RANK = {"member": 0, "columnist": 1, "author": 2}


# —— 令牌“存活/权威”判定（拆库后 auth 侧裁决；monolith deps seam 以此为单一事实源）——

# reason 码缺省；给 monolith 一个可映射的稳定 cause 以抛对应 BizError。
CAUSE_NOT_FOUND = "not_found"
CAUSE_LOCKED = "locked"
CAUSE_SESSION_REVOKED = "session_revoked"
CAUSE_PASSWORD_CHANGED = "password_changed"
CAUSE_NOT_ADMIN = "not_admin"
# 令牌刷新容忍的时钟偏差（秒），与 admin/deps 的 5s 容差一致。
_IAT_TOLERANCE = _dt.timedelta(seconds=5)


async def authorize_user(
    db: AsyncSession,
    *,
    user_id: int,
    expect_token_version: int,
    iat_ts: float | int | None,
    require_admin: bool,
) -> dict[str, object]:
    """在 auth 库内裁决一个由 `{user_id, token_version, iat(sec)}` 描述的会话是否仍存活。

    这是 monolith ``get_current_user``/``get_current_admin`` 在 seam 开启时委托的权限真值
    判定：查 User+Profile 做——存在性 / 是否锁定 / token_version 是否被提升 / updated_at 是否晚于
    签发的 iat（改密撤销）——并返回 auth 侧的【权威 account_level + role】与是否 admin 达标。
    返回 ``{ok, cause, account_level, role}``：
      ok=True  → 用户存活且（require_admin 时）已是 admin；account_level/role 用库内权威值。
      ok=False → cause 给出拒绝原因（not_found/locked/session_revoked/password_changed/not_admin）。
    只读判定（不落库改动、不发事件）；调用方（内部端点）负责 commit/close。
    """
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.profile))
    )
    user = result.scalars().first()
    if user is None:
        return {"ok": False, "cause": CAUSE_NOT_FOUND, "account_level": None, "role": None}

    if user.is_locked and user.locked_until and user.locked_until > now_iso():
        return {"ok": False, "cause": CAUSE_LOCKED, "account_level": None, "role": None}

    tv = int(expect_token_version or 0)
    if tv != int(user.token_version):
        return {"ok": False, "cause": CAUSE_SESSION_REVOKED, "account_level": None, "role": None}

    # 改密撤销：签发的 iat 时间戳须 >= user.updated_at - 5s 容差
    if user.updated_at is not None and iat_ts is not None:
        try:
            token_time = _dt.datetime.fromtimestamp(float(iat_ts), tz=_dt.UTC)
        except (TypeError, ValueError, OverflowError):
            token_time = _dt.datetime.fromtimestamp(0, tz=_dt.UTC)
        if user.updated_at - token_time > _IAT_TOLERANCE:
            return {
                "ok": False,
                "cause": CAUSE_PASSWORD_CHANGED,
                "account_level": None,
                "role": None,
            }

    profile = user.profile
    role = profile.role if profile else "member"
    account_level = str(user.account_level)
    if require_admin and account_level != "admin":
        return {"ok": False, "cause": CAUSE_NOT_ADMIN, "account_level": None, "role": None}

    return {
        "ok": True,
        "cause": None,
        "account_level": account_level,
        "role": role,
    }


def _rank_of(table: dict[str, int], value: object) -> int:
    return table.get(value, -1) if isinstance(value, str) else -1


async def grant_exam_unlock(
    db: AsyncSession,
    user_id: int,
    *,
    unlock_level: str | None,
    unlock_role: str | None,
) -> int:
    """考试通过后的单向升权：account_level/Profile.role“只升不降”。

    语义对齐 exam/service._apply_unlock。返回 1=发生实际改动（已升权）并递增过 token_version 且
    入队 user.updated；0=无改动。调用方外层事务 commit 后改动生效、事件随事务持久/relay。
    """
    if not unlock_level and not unlock_role:
        # 无任何解锁目标：无动作（与现实现一致：_apply_unlock 头部早退）。
        return 0
    return await _apply_upgrades(db, user_id, unlock_level, unlock_role)


async def _apply_upgrades(
    db: AsyncSession, user_id: int, unlock_level: str | None, unlock_role: str | None
) -> int:
    """执行单向升权：有任一真实提升才 bump token + 失效；返回是否改（0/1）。"""
    row = (
        await db.execute(
            select(User.account_level, Profile.role)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        # 用户不存在（异常态，如业务并发删号）→ 与既有 _apply_unlock 相同：无动作不报错。
        return 0

    account_level, cur_role_raw = row
    cur_role = cur_role_raw if cur_role_raw else "member"
    changed = False

    if unlock_level is not None and _rank_of(_LEVEL_RANK, unlock_level) > _rank_of(
        _LEVEL_RANK, account_level
    ):
        await db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(account_level=str(unlock_level))
        )
        changed = True
    if unlock_role is not None and _rank_of(_ROLE_RANK, unlock_role) > _rank_of(
        _ROLE_RANK, cur_role
    ):
        await db.execute(
            sa_update(Profile)
            .where(Profile.user_id == user_id)
            .values(role=str(unlock_role))
        )
        changed = True

    if changed:
        await db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(token_version=User.token_version + 1)
        )
        await db.flush()
        # 升权即身份升迁 → user.updated 失效快照 + 使旧令牌作废（镜像 auth.service.upgrade_to_normal）
        await events.notify_user_updated(db, user_id)
        return 1
    return 0


async def grant_incubation(db: AsyncSession, user_id: int) -> int:
    """纳入成员升级（projects/service._apply_incubation 的 auth 侧语义）：

    - account_level 单向升 ``admin``（已是 admin 则不升）；
    - Profile.role 仅当当前为 ``member``/空 时置为 ``incubated_member``；
    - 有任一切实变更才 bump token + 发 user.updated。返回是否改（0/1）。
    """
    row = (
        await db.execute(
            select(User.account_level, Profile.role)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        # 与既有 _apply_incubation 一致：用户不存在 → 无动作不报错。
        return 0
    account_level, cur_role_raw = row
    cur_role = cur_role_raw if cur_role_raw else "member"
    changed = False

    if account_level != "admin":
        await db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(account_level="admin")
        )
        changed = True
    if cur_role in ("member", ""):
        await db.execute(
            sa_update(Profile)
            .where(Profile.user_id == user_id)
            .values(role="incubated_member")
        )
        changed = True

    if changed:
        await db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(token_version=User.token_version + 1)
        )
        await db.flush()
        await events.notify_user_updated(db, user_id)
        return 1
    return 0


# —— 跨 realm 升权写 seam 调度（M3.B S5 C，供 exam/projects 等业务 supplier 调用） ——
#
# S5 拆库后 auth 是 users/profiles 唯一 owner（含写），业务库不再有 auth 行。故业务侧在触发
# 单向升权时**不能把自己的业务会话塞给 auth 的 grant 例程**（那样 UPDATE 会落到不存在的 users 表/
# 语义错位）。本 dispatch 把 auth 写路由到正确 realm：
#
# - ``user_http.enabled()``（auth HTTP 缝，production 拆库态 + 测试 auth_seam_realm）
#   → 经 ``user_http.grant_via_seam`` 走 auth 内部写端点，由 auth 进程在自持库/自测 auth realm 执行；
# - seam OFF（蓝绿同库态/本地单库）→ 回落本模块 :func:`grant_incubation`/`grant_exam_unlock`，
#   此时传入的 ``db`` 即含 auth 表的本地库会话（保存既有本地直写行为，零语义漂移）。
#
# 本 dispatch 保持业务 ↔ auth.service_authz 的既有依赖方向（exam/projects 已 import service_authz），
# auth 真值归属不变（写始终落在 auth realm 对应会话）。返回与底层原语同义的 ``changed``(0/1)。


async def grant_incubation_from_business(db: AsyncSession, user_id: int) -> int:
    """business supplier 触发"纳入成员升级"的单向升权（auth realm 权威写）。"""
    if user_http.enabled():
        return await user_http.grant_via_seam(kind="incubation", user_id=user_id)
    return await grant_incubation(db, user_id)


async def grant_exam_unlock_from_business(
    db: AsyncSession,
    user_id: int,
    *,
    unlock_level: str | None,
    unlock_role: str | None,
) -> int:
    """business supplier 触发"考试通过解锁"的单向升权（auth realm 权威写）。"""
    if user_http.enabled():
        return await user_http.grant_via_seam(
            kind="exam_unlock",
            user_id=user_id,
            unlock_level=unlock_level,
            unlock_role=unlock_role,
        )
    return await grant_exam_unlock(
        db, user_id, unlock_level=unlock_level, unlock_role=unlock_role
    )
