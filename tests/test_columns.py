"""columns 行为测试（M3.B S5 拆库 dual 真 PG 迁移版）。

拆库后业务库(Base 无 users)不再有 User/Profile；列显示只带裸 int owner_id/author_id。
凡"需要一名用户身份"(service 传 user_id / 或走 HTTP 鉴权) 的用例注入 ``auth_db``：
- ``_au(auth_db,...)`` 建 auth realm 用户返回稳定 ``AuthUser``，以其裸 ``.id`` 给业务列；
- HTTP 路由用例额外注入 ``auth_seam_realm``(deps 跨 realm 裁 current user role/level)。
权限点(columns.application_create / columns.*) 仍落业务 realm RolePermission，符合生产。

真双 PG(lkm / lkm_auth) schema-per-test 跑绿。
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.modules.auth.errors import AuthErr
from app.modules.auth.security import create_access_token
from app.modules.content.column_models import ColumnApplicationStatus
from app.modules.content.columns.errors import ColumnErr
from app.modules.content.columns.schemas import (
    ColumnApplicationCreate,
    ColumnApplicationInfo,
    ColumnApplicationReview,
    ColumnPostCreate,
    ColumnPostInfo,
)
from app.modules.content.columns.service import (
    create_application,
    create_post,
    get_application,
    get_column,
    get_post,
    list_applications,
    list_columns,
    list_posts,
    review_application,
)
from tests.conftest import AuthUser, auth_user_uid

# db 与 client fixture 均由 tests/conftest.py 提供（business realm schema + httpx.AsyncClient）
# auth_db 亦为 conftest：各测试在自己的 auth schema 以 auth_user_uid 造真实 auth 用户。


async def _au(
    auth_db: AsyncSession,
    username: str = "alice",
    email: str | None = None,
    nickname: str | None = None,
    account_level: str = "normal",
    role: str = "member",
) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=email or f"{username}@example.com",
        nickname=nickname,
        account_level=account_level,
        role=role,
    )


async def _grant(db: AsyncSession, role_name: str, *perms: str) -> None:
    from app.modules.admin.models import RolePermission

    for p in perms:
        db.add(RolePermission(role_name=role_name, permission=p))
    await db.flush()


async def _application(db: AsyncSession, user_id: int = 1) -> ColumnApplicationInfo:
    return await create_application(
        db,
        user_id,
        ColumnApplicationCreate(
            title="数学思维训练",
            description="面向高中生的数学思维和解题方法专栏。",
            reason="希望长期整理数学学习笔记。",
        ),
    )


async def _approved_column(db: AsyncSession, user_id: int = 1) -> dict[str, Any]:
    application = await _application(db, user_id=user_id)
    result: dict[str, Any] = await review_application(
        db,
        application.id,
        ColumnApplicationReview(
            status=ColumnApplicationStatus.APPROVED,
            review_note="方向明确，允许开设。",
        ),
        user_id,
    )
    return result["column"]


async def _post(
    db: AsyncSession, column_id: int = 1, author_id: int = 1
) -> ColumnPostInfo:
    return await create_post(
        db,
        column_id,
        ColumnPostCreate(
            title="如何建立函数思想",
            summary="从变量关系和图像理解入门函数思想。",
            content="函数思想的核心，是用变化关系理解问题。",
        ),
        author_id,
    )


class TestColumnApplications:
    async def should_create_application(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)

        application = await _application(db, user_id=user.id)

        assert application.id == 1
        assert application.user_id == user.id
        assert application.status == ColumnApplicationStatus.PENDING

    async def should_list_applications(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)
        await _application(db, user_id=user.id)

        applications = await list_applications(db)

        assert applications.total == 1
        assert applications.items[0].title == "数学思维训练"

    async def should_get_application(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)
        application = await _application(db, user_id=user.id)

        found = await get_application(db, application.id)

        assert found.id == application.id

    async def should_reject_nonexistent_application(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            await get_application(db, 999)

        assert exc.value.errcode == ColumnErr.APPLICATION_NOT_FOUND


class TestColumnReview:
    async def should_create_column_when_application_is_approved(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        application = await _application(db, user_id=user.id)

        result: dict[str, Any] = await review_application(
            db,
            application.id,
            ColumnApplicationReview(status=ColumnApplicationStatus.APPROVED),
            user.id,
        )

        assert result["application"]["status"] == ColumnApplicationStatus.APPROVED
        assert result["column"]["id"] == 1
        assert result["column"]["owner_id"] == user.id
        assert result["column"]["application_id"] == application.id

    async def should_not_create_column_when_application_is_rejected(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        application = await _application(db, user_id=user.id)

        result: dict[str, Any] = await review_application(
            db,
            application.id,
            ColumnApplicationReview(
                status=ColumnApplicationStatus.REJECTED,
                review_note="内容方向还不够清晰。",
            ),
            user.id,
        )

        assert result["application"]["status"] == ColumnApplicationStatus.REJECTED
        assert result["column"] is None
        assert (await list_columns(db)).items == []

    async def should_reject_reviewing_already_reviewed_application(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        application = await _application(db, user_id=user.id)
        review = ColumnApplicationReview(status=ColumnApplicationStatus.APPROVED)

        await review_application(db, application.id, review, user.id)

        with pytest.raises(BizError) as exc:
            await review_application(db, application.id, review, user.id)

        assert exc.value.errcode == ColumnErr.APPLICATION_ALREADY_REVIEWED


class TestColumns:
    async def should_list_columns(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)
        await _approved_column(db, user_id=user.id)

        columns = await list_columns(db)

        assert columns.total == 1
        assert columns.items[0].title == "数学思维训练"

    async def should_get_column(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)
        column = await _approved_column(db, user_id=user.id)

        found = await get_column(db, column["id"])

        assert found.id == column["id"]

    async def should_reject_nonexistent_column(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            await get_column(db, 999)

        assert exc.value.errcode == ColumnErr.NOT_FOUND


class TestColumnPosts:
    async def should_create_post(self, db: AsyncSession, auth_db: AsyncSession):
        user = await _au(auth_db)
        column = await _approved_column(db, user_id=user.id)

        post = await _post(db, column_id=column["id"], author_id=user.id)

        assert post.id == 1
        assert post.column_id == column["id"]
        assert post.author_id == user.id
        assert post.status == "published"

    async def should_list_posts_under_column(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        column = await _approved_column(db, user_id=user.id)
        await _post(db, column_id=column["id"], author_id=user.id)

        posts = await list_posts(db, column["id"])

        assert posts.total == 1
        assert posts.items[0].title == "如何建立函数思想"

    async def should_get_post_with_column_scope(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        column = await _approved_column(db, user_id=user.id)
        post = await _post(db, column_id=column["id"], author_id=user.id)

        found = await get_post(db, post.id, column_id=column["id"])

        assert found.id == post.id

    async def should_reject_post_from_wrong_column_scope(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)
        column = await _approved_column(db, user_id=user.id)
        post = await _post(db, column_id=column["id"], author_id=user.id)

        with pytest.raises(BizError) as exc:
            await get_post(db, post.id, column_id=999)

        assert exc.value.errcode == ColumnErr.POST_NOT_FOUND

    async def should_reject_post_for_nonexistent_column(
        self, db: AsyncSession, auth_db: AsyncSession
    ):
        user = await _au(auth_db)

        with pytest.raises(BizError) as exc:
            await _post(db, column_id=999, author_id=user.id)

        assert exc.value.errcode == ColumnErr.NOT_FOUND


class TestColumnRoutes:
    async def _setup_user(
        self, db: AsyncSession, auth_db: AsyncSession, grant: bool = True
    ) -> AuthUser:
        """建 auth realm 用户并授 columns.application_create（供发帖/申请路 macth）。"""
        if grant:
            await _grant(db, "normal:member", "columns.application_create")
        return await _au(
            auth_db,
            username="testuser",
            email="test@example.com",
            nickname="testuser",
            account_level="normal",
            role="member",
        )

    async def should_reject_application_without_auth_header(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        application_data: dict[str, Any] = {
            "title": "数学思维训练",
            "description": "面向高中生的数学思维和解题方法专栏。",
            "reason": "希望长期整理数学学习笔记。",
        }

        response = await client.post(
            "/api/v1/content/columns/applications", json=application_data
        )

        assert response.status_code == 403
        assert response.json()["code"] == CommonErr.FORBIDDEN

    async def should_accept_application_when_token_user_matches_body_user(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        user = await self._setup_user(db, auth_db)
        resp = await client.post(
            "/api/v1/content/columns/applications",
            headers={"Authorization": f"Bearer {user.token}"},
            json={
                "title": "数学专栏",
                "description": "整理数学学习内容。",
                "reason": "长期输出学习笔记。",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        assert resp.json()["data"]["user_id"] == user.id

    async def should_reject_applications_list_for_non_admin(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        user = await self._setup_user(db, auth_db)
        resp = await client.get(
            "/api/v1/content/columns/applications",
            headers={"Authorization": f"Bearer {user.token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == AuthErr.ACCOUNT_LEVEL_INSUFFICIENT

    async def should_reject_review_for_non_admin(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        # review 走后台 cookie 会话（require_admin_2fa）：普通用户无 admin cookie → FORBIDDEN，
        # 而非旧前台 RequireLevel(admin) 的 ACCOUNT_LEVEL_INSUFFICIENT。与 boards/projects 审核一致。
        user = await self._setup_user(db, auth_db)
        await _application(db, user_id=user.id)
        resp = await client.post(
            "/api/v1/content/columns/applications/1/review",
            json={"status": "approved"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == CommonErr.FORBIDDEN

    async def should_reject_post_for_non_owner(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        owner = await _au(auth_db, username="col_owner", role="columnist")
        await _grant(db, "normal:columnist", "columns.publish")
        column = await _approved_column(db, user_id=owner.id)
        intruder = await _au(auth_db, username="intruder", role="columnist")
        token = create_access_token(
            user_id=intruder.id, account_level="normal", role="member"
        )
        resp = await client.post(
            f"/api/v1/content/columns/{column['id']}/posts",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "越权帖", "content": "x"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == CommonErr.FORBIDDEN


def should_test():
    pass
