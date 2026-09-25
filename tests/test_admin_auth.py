"""后台登录态 seam-only / 危险操作 2FA 门禁的 HTTP 测试（S5-A2 Step1 重组）。

S5-A2 Step1 后拓扑：
- **会话写面**（login/refresh/logout/2fa）迁 AUTH 进程（auth_app），语义已在
  ``test_admin_auth_process.py`` 用 ``auth_app_client`` 逐项覆盖（这里不再机械重复）。
- **monolith** 只保留 /admin/auth/me 这一登录态读面 + 各后台保护端点，其
  ``require_admin``/``require_admin_2fa`` 改为 **seam-only**（business 不再本地读 auth
  users，改经 ``auth_seam_realm`` 把裁决指到 auth 独立库真值）；seam 未启用 → fail-closed。

本文件回归锚（均在 monolith ``client`` + ``auth_seam_realm`` seam realm 上、真 auth_db）：
- me：无 cookie 403；seam 关闭(未配 seam)即便带有效 admin cookie 也 403（fail-closed）；
  seam 开 + admin(role super_admin + admin_dashboard 持仓) → 200 返回 id/account_level/role。
- seam 裁决 revoke(non-ok) → monolith fail-closed 403（会话失效/锁定/改密撤销缝降级握接）。
- danger（content delete, require_admin_2fa）：无 mfa cookie → 401 code=4；
  带 mfa(step-up 后) cookie → 越过门禁抵达 service（不存在 item → 404）。
  会话 2FA/step-up 自身由 test_admin_auth_process 在 auth_app 覆盖；此处验证其产物 mfa
  cookie 在 monolith seam 侧被采纳为 danger 通过凭据。

RolePermission(super_admin 默认 grants) 与内容表仍在业务 realm(Base)，由 ``db`` 直插。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from auth.models import User
from tests.conftest import DB, Client, auth_user_uid

# 拆库后业务 realm 无 users：本文件一律经 auth realm 造 User + 从 auth_db ORM row mint
# admin cookie。权限点落在业务 realm(RolePermission)。auth_seam_realm 把 monolith 鉴权缝
# 指到本测 auth_db —— seam-only 的后台 require_admin/danger 据此裁决（business 绝不本地读 auth）。


async def _mk_admin(
    auth_db: AsyncSession, uname: str, *, role: str = "super_admin"
) -> User:
    """在 auth realm 造一个 admin 账号（account_level=admin）+ 指定复合角色 Profile。"""
    au = await auth_user_uid(
        auth_db,
        username=uname,
        account_level="admin",
        role=role,
    )
    row = (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()
    return row


async def _grant_super_admin(db: DB, *perms: str) -> None:
    """给 admin:super_admin 授指定权限点（每测独立 schema → 直接插无冲突）。"""
    for p in perms:
        db.add(RolePermission(role_name="admin:super_admin", permission=p))
    await db.commit()


def _set_admin_cookie(client: Client, user: User, *, mfa: bool = False) -> None:
    """把该 admin(从 auth realm ORM) 的后台 access cookie 装进 monolith client jar。

    与 auth_app 签发的 cookie 同源（settings.jwt_secret + admin_session audience），故 seam/
    require_admin_2fa 解码一致。mfa=True 表示已过危险操作 step-up(1h 信任) —— 与 test_admin_auth
    _process 经 auth /admin/auth/2fa 产物同形（cookie 契约单一）。
    """
    tok = create_admin_access_token(user, mfa_verified=mfa)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


# ===================================================================
# /auth/me —— monolith seam-only 登录态读
# ===================================================================


class TestAdminMe:
    async def should_reject_without_cookie(self, client: Client) -> None:
        """无 admin cookie → 403 Not logged（任意 seam 状态都拒）。"""
        resp = await client.get("/api/v1/admin/auth/me")
        assert resp.status_code == 403

    async def should_fail_closed_when_seam_off(
        self, db: DB, client: Client, auth_db: AsyncSession
    ) -> None:
        """seam 未启用(未开启 seam realm) → 即便带有效 admin cookie 也 fail-closed 403。"""
        admin = await _mk_admin(auth_db, "seam_off_admin")
        await _grant_super_admin(db, "admin.dashboard")
        _set_admin_cookie(client, admin)
        resp = await client.get("/api/v1/admin/auth/me")
        # business 不能本地裁决后台真值 → 一律拒（Admin auth service not configured）
        assert resp.status_code == 403
        assert resp.json()["message"] == "Admin auth service not configured"

    async def should_allow_seam_admin(
        self,
        db: DB,
        client: Client,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        """seam 开 + admin(super_admin 持仓 admin.dashboard) → me 200。"""
        admin = await _mk_admin(auth_db, "seam_me_admin")
        await _grant_super_admin(db, "admin.dashboard")
        _set_admin_cookie(client, admin)

        resp = await client.get("/api/v1/admin/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["account_level"] == "admin"
        assert body["data"]["role"] == "super_admin"
        assert body["data"]["id"] == str(admin.id)

    async def should_fail_closed_when_seam_verdict_revoked(
        self,
        client: Client,
        auth_db: AsyncSession,
        monkeypatch,
    ) -> None:
        """seam 裁决回 non-ok(改密撤销/会话失效) → monolith me fail-closed 403。

        直接替换 authz seam 判定为「该 admin 已失效」，验证 monolith 不本地回落、按不可用拒。
        """
        from app.core.config import settings as _cfg
        from auth import user_http as uh

        admin = await _mk_admin(auth_db, "seam_rev_root")
        _set_admin_cookie(client, admin)

        # seam 开：配 url+token，并让 authz 回“改密后已失效”裁决
        monkeypatch.setattr(_cfg, "auth_http_url", "http://auth-realm-test")
        monkeypatch.setattr(_cfg, "auth_http_token", "x")

        async def _revoked(*, user_id: uuid.UUID, **_: object) -> dict[str, object]:
            return {
                "ok": False,
                "cause": "password_changed",
                "account_level": None,
                "role": None,
            }

        monkeypatch.setattr(uh, "authorize_via_seam", _revoked)

        resp = await client.get("/api/v1/admin/auth/me")
        assert resp.status_code == 403  # Admin account state invalid or unavailable


# ===================================================================
# danger —— require_admin_2fa 内容删除门禁（monolith + seam）
# ===================================================================

CONTENT_DELETE = "/api/v1/admin/content/item/00000000-0000-7000-8000-000000000099"


class TestAdminDangerContentDelete:
    async def should_gate_without_mfa(
        self,
        db: DB,
        client: Client,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        """seam admin 带**未 step-up**(无 mfa) cookie 删除 → 401 code=4 MFA_REQUIRED。"""
        admin = await _mk_admin(auth_db, "danger_nomfa")
        await _grant_super_admin(db, "admin.content_review")
        _set_admin_cookie(client, admin, mfa=False)

        resp = await client.delete(CONTENT_DELETE)
        assert resp.status_code == 401
        assert resp.json()["code"] == 4  # CommonErr.MFA_REQUIRED

    async def should_pass_gate_after_stepup(
        self,
        db: DB,
        client: Client,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        """seam admin 已 step-up(mfa cookie) → 越过 2FA 门禁触达 service（不存在 item→404）。

        mfa cookie 由 auth_app 的 /admin/auth/2fa 同源签发；此处直接以同契约 cookie 代用
        （文档见 test_admin_auth_process —— auth 进程 step-up 兜底用例）。"""
        admin = await _mk_admin(auth_db, "danger_stepup")
        await _grant_super_admin(db, "admin.content_review")
        _set_admin_cookie(client, admin, mfa=True)

        resp = await client.delete(CONTENT_DELETE)
        # 非 401 MFA_REQUIRED：已穿过门禁，content 不存在 → 404
        assert resp.status_code == 404

    async def should_reject_mfa_trust_expired_cookie(
        self,
        db: DB,
        client: Client,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        """mfa_at 远超 1h 信任窗 → danger 仍拒（401 code=4），即烂 mfa 信任不可放行。"""
        import datetime

        admin = await _mk_admin(auth_db, "danger_stale")
        await _grant_super_admin(db, "admin.content_review")
        # 造一「盖章在 >1h 前」的 step-up cookie → 过期
        old = int(
            (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).timestamp()
        )
        tok = create_admin_access_token(admin, mfa_verified=True, mfa_at=old)
        client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)

        resp = await client.delete(CONTENT_DELETE)
        assert resp.status_code == 401
        assert resp.json()["code"] == 4
