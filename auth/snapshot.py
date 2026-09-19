"""auth 统一只读身份缝：UserSnapshot 冻结类型 + 单/批读 + 管理面 PII 门列表。

M3.A「读权收束」第一腿（A1，纯增量）+ 管理面腿（A4）+ 单用户读缓存腿（A6）。把散落全仓
的 ``select(User)...selectinload(User.profile)`` 展示性读取收敛到此缝。

不变量：
- ``UserSnapshot`` 是冻结定长 schema，**不含** email/phone/hashed_password 等
  敏感/PII/凭证列；敏感列只在 auth/admin 授权路径读取。
- ``_to_snap`` 假定调用方已把 ``User.profile`` 载入(selectinload/outerjoin)，
  不做二次回查；profile 缺失为空条目时回退到 username 并给空 avatar/role。
- ``banned`` 以 User.is_locked 作为现状可行替代；后续 banned 事件(改封禁语义)
  现 Task A register/auth 对齐。
- ``list_user_snapshots``（A4）是后台管理面的**分页列表**读：返回管理行
  ``UserManagementItem``（非展示 ``UserSnapshot``），**id 列序 desc + offset 分页 +
  ``total``**，以贴合其唯一消费方 admin 的 ``/admin/users`` 的 ``PageData`` 分页形态。
  ``include_pii=False``（默认）：SELECT **不投影** email/phone 两列，内存/网络零 PII；
  仅在 ``include_pii=True``（由路由在 admin 授权门槛后自决并透传）时才取这两列。
  本函数不带任何鉴权——授权由路由层（require_admin + require_permission）负责。
- 单用户读走 cache-through（A6）：``get_user_snapshot`` 命中 ``core.user_cache`` 直接返回
  ``UserSnapshot``；miss 时回填带来源版本（User.updated_at）+ 反陈旧 CAS，展示语义与直读
  DB 完全一致（diff=0）。
- 批量读（M6.5 起与单读同源）：``get_user_snapshot_batch`` 在 seam 打开时走「**一次**批量缓存读
  （L1 逐个 + L2 单次 MGET）→ 未命中项按 ``BATCH_IDS_MAX`` 分块、每块批内 singleflight →
  **一次** by-ids HTTP → 按 wire 出的真实 sv 逐条 CAS 回填」；seam 关闭时仍是**一条 SQL 查多行**。
  两条路径都**不逐 id N+1**。缓存失效由 A7 走 ``core.user_cache.invalidate_user_snap``，
  不在本缝接线。
- B1.2 HTTP seam（默认 OFF）：当配置 ``auth_http_url`` + ``auth_http_token``（见 core.config）
  时，本缝的 **miss 回填源**从「就地直读本进程业务 DB」切换成「跨 HTTP 打 AUTH 读端点」
  （``auth.user_http``；单读走 ``/users/{id}/snapshot``、批量走 ``/users/by-ids``）——在线读路径
  可由不同进程序提供。来源版本取 HTTP 信封带出的 AUTH 端真实 sv，缓存 CAS 语义不变（不捏造
  版本，不泄露面加宽）。单读 AUTH 不可达/超时/畸形 → **fail-open 回落本进程 DB**（读永不 crash、
  不以 stale 当 truth）；**批量读**（跨 realm 展示读）无本地 users 可回落 → 整块**跳过该批**
  （缺行跳过 ≠ 故障，见 ``_retrieve_fields_batch``）。seam 关闭时是既有 A6 原路径、行为逐字节
  不变（回归锚）。
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.core.singleflight as singleflight
import app.core.user_cache as user_cache
from app.core.config import settings
from app.core.metrics import user_snap_singleflight_total
from auth import user_http
from auth.models import Profile, User
from auth.schemas import ProfileInfo, ProfileRole

logger = logging.getLogger("lkm.auth.snapshot")


@dataclass(frozen=True)
class UserSnapshot:
    """业务侧固定身份读模型。不含 email/phone/凭证 等 PII/敏感列。

    ``nickname``（raw，**非 PII**，同 username/display_name 属展示身份列）为 profiles.nickname
    的逐字照搬：空白时是 None —— **不回退到 username**。这与 ``display_name``（nickname or
    username 合成）刻意保持**语义分流**：需要"展示名默认回退"的用 display_name；需要"nickname
    是否真被设置"（如 blog/articles 组 ProfileInfo 须保 blank-when-unset）的读 raw nickname。
    """

    user_id: uuid.UUID
    username: str
    display_name: str
    avatar: str | None
    role: str | None
    account_level: str
    banned: bool
    nickname: str | None


def _to_snap(user: User) -> UserSnapshot:
    """User(profile 已载入) → UserSnapshot。数据读取方须已载入 profile。"""
    p: Profile | None = user.profile
    return UserSnapshot(
        user_id=user.id,
        username=user.username,
        display_name=(p.nickname or user.username) if p else user.username,
        avatar=p.avatar if p else None,
        role=p.role if p else None,
        account_level=user.account_level,
        banned=bool(user.is_locked),
        # M3 残项 raw nickname：profiles.nickname 原样（空白即 None），**永不作
        # username 回退、永不合成**——仅供需要"nickname 是否真的被设置"的消费方
        # （如 blog/articles 组装 ProfileInfo 保 blank-when-unset）从缝读取，替代
        # 其自身的直接 Profile 行读。``display_name`` 语义不变（仍 nickname-or-username）。
        nickname=p.nickname if p else None,
    )


_SNAP_FIELDS = (
    "user_id",
    "username",
    "display_name",
    "avatar",
    "role",
    "account_level",
    "banned",
    "nickname",
)


# 批量读单次上限（M6.5）：业务侧分块大小与 AUTH 内部 by-ids 端点的入参上限**同一常量**
# （router_read 引用此处，防两侧漂移）——既限单次 HTTP 体量，也限服务端一次 in_(...) 的规模。
BATCH_IDS_MAX = 200


def _snap_to_dict(snap: UserSnapshot) -> dict[str, Any]:
    """UserSnapshot → JSON/wire 可存 dict（键即冻结字段名）。

    ``user_id`` 落为 **str**：本 dict 会经 ``user_cache.write_if_newer`` 的 ``json.dumps`` 与
    HTTP 信封（JSON 无 uuid 类型）出入，UUID 不可直接序列化。读取端一律经
    :func:`_snap_from_fields` 还原为 ``uuid.UUID``（类型注解与读取语义不变）。
    """
    data = {f: getattr(snap, f) for f in _SNAP_FIELDS}
    data["user_id"] = str(snap.user_id)
    return data


def _snap_from_fields(fields: dict[str, Any]) -> UserSnapshot:
    """冻结字段 dict（DB/缓存/HTTP 三源同构）→ UserSnapshot，``user_id`` 还原为 ``uuid.UUID``。

    缺字段 / user_id 不可解析 → 抛 KeyError/TypeError/ValueError，由调用方按各自语义处理
    （缓存判不可重建、HTTP 判不可用 fail-open；DB 源不会失败）。
    """
    data = {f: fields[f] for f in _SNAP_FIELDS}
    uid = data["user_id"]
    data["user_id"] = uid if isinstance(uid, uuid.UUID) else uuid.UUID(str(uid))
    return UserSnapshot(**data)


async def get_user_snapshot(
    db: AsyncSession, *, user_id: uuid.UUID
) -> UserSnapshot | None:
    """按 id 取单用户快照；不存在返回 None。走 cache-through（A6）+ B1.2 HTTP seam + 请求合并。

    - 命中 ``core.user_cache``：以冻结字段重建 ``UserSnapshot`` 直接返回（展示语义与直读 DB
      等价）；Redis 故障/失效 → miss。
    - miss 走 singleflight（§5.4）：同进程并发拉同一 user_id 只放一个真去调 AUTH/DB，其余复用
      其结果，防热点 key 击穿；loader 内**二次检查缓存**（可能已被先到者回填）。
    - miss 会**先捕获失效代次（``expected_epoch``）再取回填源**。回填源 = 就地直读 DB（A6，
      默认）或跨 HTTP 打 AUTH 读端点（B1.2 seam，见 ``auth.user_http``）；"取到的既有
      snapshot（或权威不存在）都先验真，再以来源版本 CAS 回填（缓存防线只对捕获 epoch 之后
      未再失效、且 sv 不陈旧的回填放行）"。回填被拒照常返回刚取到的值——缓存不影响读语义。
    - fail-open：seam 开启时若 AUTH 不可达/超时/畸形（client 抛 ``UserHttpUnavailable``），
      回退本进程 DB 直读一并返回（读永不 crash、不以 stale 当 truth）。
    """
    cached = await user_cache.read_snap(user_id)
    if cached is not None:
        return _from_cache_dict(cached)
    if settings.user_snap_singleflight_enabled:
        key = user_cache.get_user_cache_key(user_id)
        return await singleflight.run(
            key,
            lambda: _load_user_snapshot(db, user_id=user_id),
            on_role=lambda role: user_snap_singleflight_total.labels(role).inc(),
        )
    return await _load_user_snapshot(db, user_id=user_id)


async def _load_user_snapshot(
    db: AsyncSession, *, user_id: uuid.UUID
) -> UserSnapshot | None:
    """singleflight loader：二次检查缓存 → 捕获 epoch → 取回填源 → CAS 回填并返回。

    二次检查确保被合并的等待方不重复回退上游；其余语义与直路逐字节一致。
    """
    cached = await user_cache.read_snap(user_id)
    if cached is not None:
        return _from_cache_dict(cached)
    expected_epoch = await user_cache.current_epoch(user_id)

    fields, version = await _retrieve_fields(user_id, db)

    if fields is None:  # 权威不存在：不缓存缺行，直接 None（含 seam 关闭/离线沿直读路径同一语义）
        return None

    snap = _snap_from_fields(fields)
    if version is not None:
        await user_cache.write_if_newer(
            user_id, _snap_to_dict(snap), version, expected_epoch
        )
    return snap


async def _retrieve_fields(
    user_id: uuid.UUID, db: AsyncSession
) -> tuple[dict[str, Any] | None, int | None]:
    """取回填源的**冻结字段 dict + 来源版本**，失败已按 fail-open 语义收口。

    - seam 打开（``user_http.enabled()``）且 AUTH 响应：返回其冻结字段 + AUTH 端真实 sv
      （先校验可重建，user_id 不可解析的畸形体按不可用处理，不把畸形当 truth）。
    - seam 打开但 AUTH 不可用/畸形（抛 ``UserHttpUnavailable``）：记降级日志，**回落 DB**
      （就地 ``select(User)...`` + ``_to_snap`` + ``version_of_updated_at``），不把失败当 None。
    - seam 关闭（默认）：就地 DB 直读（既有 A6 原路径）。
    """
    if user_http.enabled():
        try:
            fields, version = await user_http.fetch_user_http_payload(user_id)
        except user_http.UserHttpUnavailable:
            logger.warning("auth_http read failed uid=%s; fail-open to local DB", user_id)
        else:
            if fields is None:  # 权威不存在：不回落 DB、不缓存缺行
                return None, version
            try:
                _snap_from_fields(fields)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "auth_http malformed snapshot uid=%s; fail-open to local DB",
                    user_id,
                )
            else:
                return fields, version
    return await _fetch_fields_from_db(user_id, db)


async def _fetch_fields_from_db(
    user_id: uuid.UUID, db: AsyncSession
) -> tuple[dict[str, Any] | None, int | None]:
    """就地直读业务 DB：User(+profile) → 冻结字段 dict + 来源版本（A6 原路径的抽出的查询体）。

    返回 ``(fields_dict=None, version=None)`` 表示该用户不存在（权威）；否则带来源版本。
    """
    row = (
        await db.execute(
            select(User).where(User.id == user_id).options(selectinload(User.profile))
        )
    ).scalar_one_or_none()
    if row is None:
        return None, None
    snap = _to_snap(row)
    version = user_cache.version_of_updated_at(row.updated_at) if row.updated_at else None
    return _snap_to_dict(snap), version


def _from_cache_dict(data: dict[str, Any]) -> UserSnapshot | None:
    """缓存 dict → UserSnapshot。字段残缺/多余/id 不可解析一律判不可重建 → None（回落 DB，杜绝脏缓存透出）。"""
    try:
        return _snap_from_fields(data)
    except (KeyError, TypeError, ValueError):
        return None


async def get_user_snapshot_batch(
    db: AsyncSession, *, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, UserSnapshot]:
    """按 id 列表批量取快照；不存在的 id 不在结果里。空列表返回空 dict。"""
    if not user_ids:
        return {}
    fields_map = await _retrieve_fields_batch(list(set(user_ids)), db)
    out: dict[uuid.UUID, UserSnapshot] = {}
    for uid, f in fields_map.items():
        try:
            out[uid] = _snap_from_fields(f)
        except (KeyError, TypeError, ValueError):
            continue  # 坏行跳过（与缺行同语义），不因单行畸形整批失败
    return out


async def _fetch_fields_batch_from_db(
    user_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, tuple[dict[str, Any], int | None]]:
    """就地**单查询**批量直读：``{id: (冻结字段 dict, 来源版本)}``；缺行不在结果里。

    与 :func:`_fetch_fields_from_db` 同源（同一 ``_to_snap`` + 版本推导），差别只在一查多行。
    供两处共用：seam 关闭时的批量读、AUTH 内部 by-ids 读端点（M6.5）。
    """
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(User)
            .where(User.id.in_(user_ids))
            .options(selectinload(User.profile))
        )
    ).scalars()
    out: dict[uuid.UUID, tuple[dict[str, Any], int | None]] = {}
    for u in rows:
        version = (
            user_cache.version_of_updated_at(u.updated_at) if u.updated_at else None
        )
        out[u.id] = (_snap_to_dict(_to_snap(u)), version)
    return out


def _batch_singleflight_key(chunk: list[uuid.UUID]) -> str:
    """批内合并键（与单读键不同域）：排序分块后同一集合必得同键（首/末 id + 长度）。

    ``chunk`` 由调用方（:func:`_retrieve_fields_batch`）在 ``sorted(set(user_ids))`` 上切片，
    恒为升序；``uuid.UUID`` 可全序比较（按 128-bit int，规范 str 亦同序），故 ``chunk[0]``/
    ``chunk[-1]`` 就是该块的最小/最大 id，同集合必得稳定键，无需改按字符串排序。
    """
    return f"{user_cache.get_user_cache_key(chunk[0])}:batch:{chunk[-1]}:{len(chunk)}"


async def _load_batch_uncached(
    user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """单块 loader（M6.5）：二次检查缓存 → **一次**批量 HTTP → 带来源版本逐条 CAS 回填。

    与单读 loader 同纪律：先捕获各 id 的失效代次（``expected_epoch``）再取回填源，写回由
    ``write_if_newer`` 做「sv 不陈旧 + 期间未失效」双校验；被拒写不影响返回值（缓存不改读语义）。
    """
    out = await user_cache.read_snaps(user_ids)
    pending = [uid for uid in user_ids if uid not in out]
    if not pending:
        return out
    epochs = {uid: await user_cache.current_epoch(uid) for uid in pending}
    try:
        fetched = await user_http.fetch_users_http_batch(pending)
    except user_http.UserHttpUnavailable:
        logger.warning("auth_http batch read failed n=%s; skip rows", len(pending))
        return out
    for uid, (fields, source_version) in fetched.items():
        if fields is None:  # 权威不存在：不缓存缺行，也不入结果（缺行 ≠ 故障）
            continue
        out[uid] = fields
        if source_version is not None:
            await user_cache.write_if_newer(uid, fields, source_version, epochs[uid])
    return out


async def _retrieve_fields_batch(
    user_ids: list[uuid.UUID], db: AsyncSession
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量取回填源**冻结字段 dict**（跨 realm 语义，同单读）。

    - seam 打开（``user_http.enabled()``）：①**一次批量缓存读**（L1 逐个 + L2 单次 MGET）→
      ②未命中项按 ≤`BATCH_IDS_MAX` 分块，每块经**批内 singleflight**（并发同块只放一个去拉）
      → **一次 HTTP** 批量端点 → ③命中结果带来源版本逐条 CAS 回填缓存。权威缺(``data=null``)
      或 HTTP 不可用 → **跳过该 id**（不入结果，配合业务展示读取方自己的 ``.get(id,"")``
      语义降级为空白展示）；**绝不让该 id 回落业务 db 查 User**（M3.B S5 拆库后业务 realm
      无 users，读了会 UndefinedTable）。（不抛错：跨 realm 下"批量展示读"无本地回退可抛，
      纪律=缺行跳过 ≠ 故障。）
    - seam 关闭（默认）：就地 **SQL 单查询批量**读本进程 db（既有 A6 原路径、非 N+1）。
    """
    if not user_http.enabled():
        rows = await _fetch_fields_batch_from_db(user_ids, db)
        return {uid: fields for uid, (fields, _sv) in rows.items()}

    ids = sorted(set(user_ids))
    out: dict[uuid.UUID, dict[str, Any]] = await user_cache.read_snaps(ids)
    missing = [uid for uid in ids if uid not in out]
    for start in range(0, len(missing), BATCH_IDS_MAX):
        chunk = missing[start : start + BATCH_IDS_MAX]
        try:
            if settings.user_snap_singleflight_enabled:
                part = await singleflight.run(
                    _batch_singleflight_key(chunk),
                    lambda c=chunk: _load_batch_uncached(c),
                    on_role=lambda role: user_snap_singleflight_total.labels(
                        role
                    ).inc(),
                )
            else:
                part = await _load_batch_uncached(chunk)
        except Exception:
            # 展示型批量读不得因单块异常整体失败：记日志、跳过该块（缺行跳过语义）。
            logger.exception("auth_http batch load failed n=%s; skip rows", len(chunk))
            continue
        out.update(part)
    return out


def profile_info_from_snap(snap: UserSnapshot) -> ProfileInfo | None:
    """快照缝 → ``ProfileInfo`` DTO（M3.A残项：blog/articles 组装作者资料时脱离直读 Profile）。

    - ``nickname`` 取 snap 的 **raw nickname**（原样，空白即 None），**且不回退 username** ——
      保持 blog/articles ProfileInfo blank-when-unset 语义（这与 display_name 的合成回退刻意分流）。
    - ``role`` 由 snap.role 字符串转 ``ProfileRole``（None → 默认 MEMBER）。
    - 无 Profile 的用户（snap.role 缺失 → 原 repo 路径不产出 ProfileInfo）返回 None，保持原先
      ``profiles.get(uid)`` 对 no-profile 用户落 None 的语义；其余字段 avatar/nickname 逐字节照搬。
    ``ProfileRole``/nickname/avatar 均非 PII，展示方本就承载；本转换不含 email/phone/凭证。
    """
    if snap.role is None:  # profile.role 非空(NOT NULL default='member')，None 即无 Profile 行
        return None
    try:
        role = ProfileRole(snap.role)
    except ValueError:
        role = ProfileRole.MEMBER
    return ProfileInfo(nickname=snap.nickname, avatar=snap.avatar, role=role)


@dataclass(frozen=True)
class UserManagementItem:
    """后台管理面用户行：管理列(id/username/account_level/is_locked/created_at)恒定。

    刻意**不是**展示型 ``UserSnapshot``：管理行是 admin 治理列表的必要字段（created_at/
    is_locked 等展示缝不暴露），且可条件承载 PII。email/phone 字段在 ``include_pii=False``
    的投影路径**根本不被 SELECT**，恒为 None（默认构造不读写 User 的 PII 列）；只有
    ``include_pii=True`` 的项目才填充。本类型仅供管理授权读，不入展示缝——评论侧等展示
    消费方接触到的仍是零 PII 的 ``UserSnapshot``。
    """

    id: uuid.UUID
    username: str
    account_level: str
    is_locked: bool
    created_at: datetime.datetime
    # PII（默认隐藏；include_pii=True 才填充）
    email: str | None = None
    phone: str | None = None


async def list_user_snapshots(
    db: AsyncSession,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    include_pii: bool = False,
) -> tuple[list[UserManagementItem], int]:
    """管理面分页列表读（A4）。返回 ``(id 列序 desc 的一页 rows, 过滤后 total)``。

    - **本函数不做任何授权**。admin ``require_admin + require_permission`` 门槛在路由层
      判定后自行决定 ``include_pii``（授权与 gate 都归路由），此处仅接收布尔。
    - ``q`` 关键字仅按 ``User.username`` 匹配（与既有 admin 行为一致，且邮箱不展示时
      不按邮箱筛选以免泄露式枚举）。
    - ``include_pii=False``（默认）：SELECT 不投影 email/phone，PII 零接触；
      True 时才取邮件/手机。创建时间/is_locked/account_level 非 PII（绑定约束仅
      email/phone/凭证为 PII），管理行恒带。
    - 分页对接 admin 的 ``PageData``：offset + limit + 返回过滤后 total（路由据此算
      page/pages）。排序用 ``id desc`` 与既有 admin 列表一致、分页键稳定。
    """
    # 计数：与行查询同 predicate(q 仅按 username)，路由据此算 page/pages。
    count_q = select(func.count(User.id))
    cond = User.username.ilike(f"%{q}%") if q else None
    if cond is not None:
        count_q = count_q.where(cond)
    total = int((await db.execute(count_q)).scalar_one() or 0)


    if include_pii:
        # 显式投影 email/phone：PII 只能在 gate 打开时接触这两列
        stmt = select(
            User.id,
            User.username,
            User.account_level,
            User.is_locked,
            User.created_at,
            User.email,
            User.phone,
        )
        if cond is not None:
            stmt = stmt.where(cond)
        stmt = stmt.order_by(User.id.desc()).offset(offset).limit(limit)
        rows = (await db.execute(stmt)).all()
        items = [
            UserManagementItem(
                id=r[0],
                username=r[1],
                account_level=str(r[2]),
                is_locked=bool(r[3]),
                created_at=r[4],
                email=r[5],
                phone=r[6],
            )
            for r in rows
        ]
    else:
        # no-pii：SELECT 不投影 email/phone，内存/网络零 PII，字段落默认 None
        stmt = select(
            User.id,
            User.username,
            User.account_level,
            User.is_locked,
            User.created_at,
        )
        if cond is not None:
            stmt = stmt.where(cond)
        stmt = stmt.order_by(User.id.desc()).offset(offset).limit(limit)
        rows = (await db.execute(stmt)).all()
        items = [
            UserManagementItem(
                id=r[0],
                username=r[1],
                account_level=str(r[2]),
                is_locked=bool(r[3]),
                created_at=r[4],
            )
            for r in rows
        ]
    return items, total


# ---------------------------------------------------------------------------
# S5-A2 Step2：后台 admin 数据面板只读计数/趋势（auth authoritative，零 PII）
#
# 拆库后 monolith biz admin reader 不再本地跨库 count/分组读 auth ``users``（users 已在
# auth 库 lkm_auth）。以下两函数是 auth 域的**只读数字缝**：总数 / 按 UTC 日注册增量——
# 纯聚合**数字**，无 email/phone/凭证 等 PII。由 biz admin reader 以 **auth 库会话**
# （``auth.db.session.get_auth_session`` 産出）调用本缝，取 auth authoritative。
# 本组函数同 ``list_user_snapshots``：不带鉴权，授权在路由层（require_admin +
# require_permission）负责；``db`` 形参即调用方传入的 auth 会话（本缝不做 realm 假设——
# A4 list_user_snapshots 同款，db 由调用方给 auth 的 session）。数字聚合只依赖
# User.created_at / id 等非 PII 列，PII-safe。
# ---------------------------------------------------------------------------


async def count_active_users(db: AsyncSession) -> int:
    """注册用户总数（后台 /admin/stats 的 user_count 真值，auth authoritative）。

    只 ``COUNT(User.id)``——纯聚合数字，零 PII。调用方需传 **auth 库会话**。
    """
    return int((await db.execute(select(func.count(User.id)))).scalar_one() or 0)


async def user_count_by_day(
    db: AsyncSession,
    *,
    start: datetime.date,
    days: int,
) -> dict[datetime.date, int]:
    """按 UTC 日分桶统计注册增量（后台 /admin/stats/trend 的 user_delta 真值）。

    返回 ``{date: 当日新增用户数}``，只覆盖 ``[start, start+days)`` 窗口；窗口内缺日由路由
    以日期序列循环补 0。

    PG 的 ``func.date(timestamptz)`` 会先按会话时区(本地+08)取日，与“以 UTC 今天为基准”
    偏移一天，故先 ``AT TIME ZONE 'UTC'`` 变 naive-UTC 再取日（与单体既有 admin_trend
    语义一致，跨天不 flaky）。本函数只返回落在窗口内、增量 >0 的日期 → 计数，零 PII。
    """
    end = start + datetime.timedelta(days=days)
    day_expr = func.date(func.timezone("UTC", User.created_at))
    # 窗口边界须用 **UTC aware datetime** 而非 ``date``：asyncpg 绑定 date 时 PG 会按
    # 会话时区（东八区部署实测 +08）把它转 timestamptz，使 [start, end) 整体偏移 8 小时
    # ——右界被提前到 UTC 前一日 16:00，落在该 8 小时内新建的用户被漏计（本地凌晨跑测试
    # 必现 user_delta=0）。用 aware datetime 比较既精确，又保留 created_at 列索引可用。
    start_dt = datetime.datetime.combine(start, datetime.time.min, tzinfo=datetime.UTC)
    end_dt = datetime.datetime.combine(end, datetime.time.min, tzinfo=datetime.UTC)
    stmt = (
        select(day_expr.label("d"), func.count())
        .where(User.created_at >= start_dt, User.created_at < end_dt)
        .group_by("d")
    )
    rows = (await db.execute(stmt)).all()
    out: dict[datetime.date, int] = {}
    for r in rows:
        raw = r[0]
        if raw is None:
            continue
        r0: str = raw if isinstance(raw, str) else str(raw)
        with contextlib.suppress(ValueError):
            out[datetime.date.fromisoformat(r0[:10])] = int(r[1] or 0)
    return out
