"""A1 读缝：UserSnapshot 冻结类型 + 单/批读的 TDD 测试。

- test_snapshot_fields_frozen：brief 既定用例，验冻结 dataclass 定义存在。
- 其余用例直连 auth/snapshot 读缝，对着 conftest 全局内存库 `db` 断言
  UserSnapshot 各字段与 _to_snap 口径（display_name = nickname or username 回退、
  avatar/role 依 profile 存在性、banned = is_locked、account_level=str 直透）。
- conftest db=autoflush=False；插入靠显式 flush()，同一会话内读可见，无需 commit。
- 回归基线：pytest -m "not integration" 全绿。本文件不引入新 fixture，复用 conftest。
"""

import uuid
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Profile, User
from app.modules.auth.security import hashpwd
from app.modules.auth.snapshot import (
    UserSnapshot,
    get_user_snapshot,
    get_user_snapshot_batch,
)
from tests.conftest import DB

# 固定 uuid7 形态常量（跨断言复用；第 3 段以 7 开头、第 4 段以 8 开头）
_UID = uuid.UUID("00000000-0000-7000-8000-000000000001")


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """本文件读缝直对 auth realm（M3.B S5 拆库）。

    conftest 的 ``db`` 现只建 Base（53 表、**无 users**）；``auth_db`` 才建 AuthBase（18
    表，含 users/profiles）。本模块是 auth/snapshot 读缝的核心测试，User/Profile 归属 auth
    realm → 把本模块内所有 ``db``（签名里的 fixture 名）重绑定到 conftest ``auth_db``：
    造数(_mk_user)与读缝(get_user_snapshot/_batch)同一 auth session，SQL 落在真实 AuthBase，
    语义=跨 realm 取 auth 真值。business ``db`` 本模块用不到。
    """
    return auth_db


# ---- 基础冻结类型存在性 (brief Step 1) ----


def test_snapshot_fields_frozen() -> None:
    snap = UserSnapshot(
        user_id=_UID,
        username="bob",
        display_name="Bob",
        avatar=None,
        role=None,
        account_level="local",
        banned=False,
        nickname=None,
    )
    assert snap.user_id == _UID
    # frozen dataclass 赋值抛 FrozenInstanceError（冻结只读不可变不变量）
    with pytest.raises(FrozenInstanceError):
        snap.display_name = "mutate"  # ty: ignore[invalid-assignment]


# ---- 造数辅助：同一 db 会话内 flush，返回 user_id ----


async def _mk_user(
    db: AsyncSession,
    username: str,
    *,
    nickname: str | None = None,
    role: str | None = None,
    account_level: str = "normal",
    locked: bool = False,
    with_profile: bool = True,
) -> uuid.UUID:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret123456"),
        account_level=account_level,
        is_locked=locked,
    )
    db.add(user)
    await db.flush()
    if with_profile:
        db.add(Profile(user_id=user.id, nickname=nickname, role=role or "member"))
        await db.flush()
    return user.id


# ---- 读缝对 DB 的单/批读 ----


async def test_get_user_snapshot_missing_returns_none(db: DB) -> None:
    snap = await get_user_snapshot(db, user_id=uuid.uuid4())
    assert snap is None


async def test_get_user_snapshot_projects_display_columns(db: DB) -> None:
    uid = await _mk_user(db, "alice", nickname="爱丽丝", account_level="admin")
    snap = await get_user_snapshot(db, user_id=uid)
    assert snap is not None
    assert snap.user_id == uid
    assert snap.username == "alice"
    assert snap.display_name == "爱丽丝"
    assert snap.account_level == "admin"
    assert snap.role == "member"
    assert snap.banned is False
    # UserSnapshot 绝不携带 PII/凭证列
    assert not hasattr(snap, "email")
    assert not hasattr(snap, "phone")
    assert not hasattr(snap, "hashed_password")


async def test_display_name_falls_back_to_username_when_no_nickname(db: DB) -> None:
    uid = await _mk_user(db, "bob", nickname=None)
    snap = await get_user_snapshot(db, user_id=uid)
    assert snap is not None
    assert snap.display_name == "bob"


async def test_avatar_role_none_without_profile(db: DB) -> None:
    uid = await _mk_user(db, "bare", nickname=None, with_profile=False)
    snap = await get_user_snapshot(db, user_id=uid)
    assert snap is not None
    assert snap.display_name == "bare"
    assert snap.avatar is None
    assert snap.role is None


async def test_banned_derived_from_is_locked(db: DB) -> None:
    uid = await _mk_user(db, "ken", nickname="Ken", locked=True)
    snap = await get_user_snapshot(db, user_id=uid)
    assert snap is not None
    assert snap.banned is True


async def test_batch_returns_partial_and_keeps_projection(db: DB) -> None:
    a = await _mk_user(db, "u_a", nickname="Alpha")
    b = await _mk_user(db, "u_b", nickname=None, account_level="admin")
    res = await get_user_snapshot_batch(db, user_ids=[a, b, uuid.uuid4()])
    assert set(res) == {a, b}
    assert res[a].display_name == "Alpha"
    assert res[b].display_name == "u_b"
    assert res[b].account_level == "admin"


async def test_batch_empty_returns_empty_dict(db: DB) -> None:
    assert await get_user_snapshot_batch(db, user_ids=[]) == {}


# ---- M3.A 残项：raw nickname 语义分叉（nickname 原样可空，display 才回退 username） ----


async def test_raw_nickname_set_is_verbatim_on_single_and_batch(db: DB) -> None:
    uid = await _mk_user(db, "nimbo", nickname="真·昵称")
    single = await get_user_snapshot(db, user_id=uid)
    assert single is not None
    assert single.nickname == "真·昵称"
    assert single.display_name == "真·昵称"

    batch_row = (await get_user_snapshot_batch(db, user_ids=[uid]))[uid]
    assert batch_row.nickname == "真·昵称"
    assert batch_row.display_name == "真·昵称"


async def test_raw_nickname_blank_stays_none_while_display_falls_back(db: DB) -> None:
    """语义分叉锚：nickname 空白(DB None) → seam.nickname is None（不回退），
    display_name 才回退 username。批量路径同一口径。"""
    uid = await _mk_user(db, "blanknick", nickname=None)
    single = await get_user_snapshot(db, user_id=uid)
    assert single is not None
    assert single.nickname is None          # raw 原样空白，不合成 username
    assert single.display_name == "blanknick"  # display 语义不变：回退 username

    batch_row = (await get_user_snapshot_batch(db, user_ids=[uid]))[uid]
    assert batch_row.nickname is None
    assert batch_row.display_name == "blanknick"


async def test_snapshot_has_exact_frozen_fields(db: DB) -> None:
    uid = await _mk_user(db, "carl", nickname="Carl")
    snap = await get_user_snapshot(db, user_id=uid)
    assert snap is not None
    assert isinstance(snap, UserSnapshot)
    # 切缝安全网：只允许固定读字段，杜绝 PII 混入。nickname(raw, 非 PII 展示身份列)随
    # M3.A 残项加入冻结集；display_name 合成回退语义不变，nickname 原样可空。
    assert set(snap.__dataclass_fields__) == {
        "user_id",
        "username",
        "display_name",
        "avatar",
        "role",
        "account_level",
        "banned",
        "nickname",
    }
