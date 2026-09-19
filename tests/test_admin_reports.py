"""后台举报列表端点 /admin/reports 的 HTTP 集成测试。

覆盖：非 admin 拒绝、admin(经 seam→auth authoritative)列表返回、按 status 过滤、空列表。

S5-A2 Step2：users/profiles 真值在 auth 库；后台鉴权 seam(``auth_seam_realm``) 指 auth_db
裁决 admin；权限点 RolePermission 落 biz ``db``。举报(Report)本在 biz ``db`` 造。后台 cookie
以 create_admin_access_token 直接置入 client jar（不触发后台 login HTTP）。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import Report, RolePermission
from app.modules.rbac.permissions import Permission
from auth.models import User
from tests.conftest import auth_user_uid  # type: ignore[attr-defined]


async def _mk_admin(
    auth_db: AsyncSession, username: str = "root"
) -> User:
    au = await auth_user_uid(
        auth_db,
        username=username,
        account_level="admin",
        role="super_admin",
        with_token=False,
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


async def _mk_member(
    auth_db: AsyncSession, username: str
) -> User:
    au = await auth_user_uid(
        auth_db, username=username, account_level="normal", with_token=False
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


def _set_admin_cookie(client: AsyncClient, user: User) -> None:
    client.cookies.set(
        COOKIE_NAME,
        create_admin_access_token(user),
        path=COOKIE_PATH,
    )


async def _grant(db: AsyncSession, perm: Permission) -> None:
    """给 admin:super_admin 授指定权限点（幂等，复刻 super_admin DEFAULT_GRANTS 的 reports 域）。"""
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == "admin:super_admin",
            RolePermission.permission == perm.value,
        )
    )
    if exists is None:
        db.add(RolePermission(role_name="admin:super_admin", permission=perm.value))
    await db.flush()


async def _seed_reports(db: AsyncSession) -> None:
    db.add_all(
        [
            Report(
                type="post",
                target_id="post-1",
                target_title="垃圾广告帖",
                reporter_name="A",
                reason="营销推广",
                status="pending",
            ),
            Report(
                type="comment",
                target_id="post-2",
                target_title="恶意评论",
                reporter_name="B",
                reason="人身攻击",
                status="pending",
            ),
            Report(
                type="file",
                target_id="file-7",
                target_title="版权文件",
                reporter_name="C",
                reason="版权",
                status="resolved",
            ),
        ]
    )
    await db.commit()


class TestAdminReports:
    async def should_reject_non_admin(
        self, client: AsyncClient, auth_db: AsyncSession
    ) -> None:
        member = await _mk_member(auth_db, "member1")
        _set_admin_cookie(client, member)
        resp = await client.get("/api/v1/admin/reports")
        assert resp.status_code in (401, 403)

    async def should_list_reports_for_admin(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _seed_reports(db)
        await _grant(db, Permission.admin_reports_view)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/reports")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["total"] == 3
        assert len(body["data"]["items"]) == 3
        first = body["data"]["items"][0]
        assert set(first.keys()) >= {
            "id",
            "type",
            "target_id",
            "target_title",
            "reporter_name",
            "reason",
            "status",
        }

    async def should_filter_reports_by_status(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _seed_reports(db)
        await _grant(db, Permission.admin_reports_view)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/reports", params={"status": "pending"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 2
        assert all(item["status"] == "pending" for item in body["data"]["items"])

    async def should_return_empty_when_no_reports(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _grant(db, Permission.admin_reports_view)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/reports")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 0
        assert body["data"]["items"] == []
