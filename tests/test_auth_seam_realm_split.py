"""M3.B S5 C：auth seam 跨 realm 落点回归锚（业务不摸业务 users、读 auth 真值）。

拆库后业务库(Base=53 表)已无 users/profiles；业务代码经 seam_*_realm fixture把表现读/鉴权
引到 auth realm（本测 auth_db=AuthBase 含 users/profiles）。据此：

1. 业务 HTTP 鉴权（登录身份裁决）走 ``auth.deps`` seam：``authorize_via_seam`` 替身
   （``tests.conftest.auth_seam_realm``）读本测 auth_db 裁 verdict —— 缺省不建任何业务用户。
2. display-name 展示读（批量）走 ``snapshot.get_user_snapshot_batch``：seam 开启时逐 user_id
   走 ``fetch_user_http_payload`` 替身读 auth_db（全新 `_retrieve_fields_batch` 缝路径），
   业务 realm 无 users 也绝不 UndefinedTable。

真双 PG（monolith=lkm / auth=lkm_auth）各建 schema 亦可跑。
"""
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth import deps as auth_deps
from app.modules.auth.models import User
from app.modules.auth.snapshot import get_user_snapshot_batch
from tests.conftest import DB, auth_user_uid


async def _assert_business_select_user_missing(db: AsyncSession) -> bool:
    """业务 realm(Base) 对 ``select(User)`` 应 UndefinedTable；返回是否如预期缺失。"""
    try:
        await db.execute(select(User))
    except (ProgrammingError, OperationalError):
        return True
    return False


async def test_business_realm_db_has_no_users_table(db: DB, auth_db: AsyncSession) -> None:
    """证明业务 realm(Base) 真无 users：任何 select(User) 到业务会话必崩 → 业务只得走缝。"""
    assert await _assert_business_select_user_missing(db)  # 业务 realm 无 users → ProgrammingError


async def test_authz_seam_resolves_current_user_from_auth_realm(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """业务 HTTP 登录：deps seam(替身读 auth_db) 裁正常 normal/member 用户 → CurrentUser。

    关键：业务 db 本就没有 users；若 seam 偷偷回落业务 db 必定 UndefinedTable。这里能通过
    即证明鉴权真值在 auth realm 取到、且没碰业务 users。
    """
    from app.modules.auth.security import create_access_token

    assert await _assert_business_select_user_missing(db)  # 前置：业务 realm 无 users

    au = await auth_user_uid(
        auth_db, username="alice", nickname="爱丽丝", account_level="normal", role="member"
    )
    token = create_access_token(
        user_id=au.id,
        account_level=au.account_level,
        role="member",
        token_version=0,
    )
    cu = await auth_deps._resolve_current_user(token, db)
    assert cu.id == au.id
    assert cu.account_level == "normal"
    assert cu.role == "member"


async def test_snapshot_batch_display_reads_from_auth_realm(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """业务展示读：get_user_snapshot_batch(业务 db) seam 开 → display_name 来自 auth realm。

    即便业务 db 无 users，批量展示名也经缝从 auth_db 取到 nickname（display=爱丽丝），不崩。
    """
    assert await _assert_business_select_user_missing(db)

    au = await auth_user_uid(
        auth_db, username="bob", nickname="狮子bob", account_level="normal", role="member"
    )
    snaps = await get_user_snapshot_batch(db, user_ids=[au.id, 99999])
    assert 99999 not in snaps  # 权威缺：缺行跳过（不回落、不缓存缺行）
    assert au.id in snaps
    assert snaps[au.id].display_name == "狮子bob"
    assert snaps[au.id].username == "bob"
