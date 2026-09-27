"""乐观锁（蓝图 §6.1）：业务表 CAS 更新冲突返 409 + 当前值。

覆盖：
- ``AsyncRepository.update_cas`` 命中 → version+1；版本过期 → ``VersionConflictError``，
  ``current`` 带服务端当前 version + 关键字段，且**过期写未落库**（可证伪）。
- 请求体 ``version`` 可选：不传 = 不校验（向后兼容）但仍递增版本；传了不符 = 冲突。
- 端点层：articles PATCH / boards PATCH 冲突 → HTTP 409 + body.data 是当前值。
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from app.db.repository import VersionConflictError
from app.modules.admin.models import RolePermission
from app.modules.content.articles.models import Article
from app.modules.content.articles.schemas import (
    ArticleCreate,
    ArticleUpdate,
    CategoryCreate,
)
from app.modules.content.articles.service import (
    create_article_ex,
    create_category_ex,
    update_article_ex,
)
from app.modules.content.boards.schemas import BoardCreate, BoardUpdate
from app.modules.content.boards.service import create_board_ex, update_board_ex
from tests.conftest import AuthUser, auth_user_uid


async def _au(
    auth_db: AsyncSession,
    username: str,
    level: str = "normal",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建用户并 mint 对应 (account_level, role) 的 token。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level=level,
        role=role,
    )


async def _category(db: AsyncSession, slug: str = "news") -> uuid.UUID:
    return (await create_category_ex(db, CategoryCreate(slug=slug, title=slug))).id


async def _article(db: AsyncSession, category_id: uuid.UUID, slug: str = "a1") -> None:
    await create_article_ex(
        db,
        ArticleCreate(
            title="原题", slug=slug, content="正文", category_id=category_id, status="draft"
        ),
    )


async def _reload(db: AsyncSession, slug: str) -> Article:
    return (await db.execute(select(Article).where(Article.slug == slug))).scalars().one()


class TestArticleOptimisticLock:
    """service 层文章 CAS。"""

    async def test_new_article_starts_at_version_1(self, db: AsyncSession) -> None:
        await _article(db, await _category(db))
        assert (await _reload(db, "a1")).version == 1

    async def test_matching_version_updates_and_bumps(self, db: AsyncSession) -> None:
        await _article(db, await _category(db))
        out = await update_article_ex(
            db, "a1", ArticleUpdate(title="新题", version=1), is_super=True
        )
        assert out.title == "新题" and out.version == 2
        assert (await _reload(db, "a1")).version == 2

    async def test_stale_version_conflicts_with_current_value(
        self, db: AsyncSession
    ) -> None:
        await _article(db, await _category(db))
        await update_article_ex(
            db, "a1", ArticleUpdate(title="第一次", version=1), is_super=True
        )
        # 落后一个版本的写必须被拒，并带出服务端当前值
        with pytest.raises(VersionConflictError) as e:
            await update_article_ex(
                db, "a1", ArticleUpdate(title="过期写", version=1), is_super=True
            )
        assert e.value.errcode == CommonErr.VERSION_CONFLICT
        assert e.value.current["version"] == 2
        assert e.value.current["slug"] == "a1"
        assert e.value.current["title"] == "第一次"
        # 证伪：冲突写**没有**落库
        row = await _reload(db, "a1")
        assert row.title == "第一次" and row.version == 2

    async def test_without_version_is_backward_compatible(self, db: AsyncSession) -> None:
        """旧客户端不带 version：仍能改，且版本号照样递增（不校验≠不记账）。"""
        await _article(db, await _category(db))
        out = await update_article_ex(db, "a1", ArticleUpdate(title="无版本"), is_super=True)
        assert out.title == "无版本" and out.version == 2

    async def test_stale_write_after_versionless_write_conflicts(
        self, db: AsyncSession
    ) -> None:
        """不带版本的写入也递增版本 → 随后带旧版本的写照样冲突。"""
        await _article(db, await _category(db))
        await update_article_ex(db, "a1", ArticleUpdate(title="A"), is_super=True)
        with pytest.raises(VersionConflictError):
            await update_article_ex(
                db, "a1", ArticleUpdate(title="B", version=1), is_super=True
            )


class TestBoardOptimisticLock:
    """service 层板块 CAS。"""

    async def test_matching_version_and_conflict(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        au = await _au(auth_db, "owner")
        board = await create_board_ex(db, BoardCreate(slug="olb", title="板"), au.id)
        assert board.version == 1

        out = await update_board_ex(
            db, board.id, au.id, BoardUpdate(title="新板", version=1)
        )
        assert out.title == "新板" and out.version == 2

        with pytest.raises(VersionConflictError) as e:
            await update_board_ex(db, board.id, au.id, BoardUpdate(title="过期", version=1))
        assert e.value.errcode == CommonErr.VERSION_CONFLICT
        assert e.value.current["version"] == 2
        assert e.value.current["slug"] == "olb"
        assert e.value.current["title"] == "新板"


class TestOptimisticLockHTTP:
    """端点层：冲突转成 409 + body.data 当前值。"""

    async def test_article_patch_conflict_returns_409_with_data(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        db.add(
            RolePermission(role_name="admin:super_admin", permission="articles.publish")
        )
        await db.flush()
        await _article(db, await _category(db), slug="conflict-1")
        await update_article_ex(
            db, "conflict-1", ArticleUpdate(title="已改", version=1), is_super=True
        )
        au = await _au(auth_db, "root", level="admin", role="super_admin")
        headers = {"Authorization": f"Bearer {au.token}"}

        resp = await client.patch(
            "/api/v1/articles/conflict-1",
            headers=headers,
            json={"title": "过期写", "version": 1},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == CommonErr.VERSION_CONFLICT
        assert body["data"]["version"] == 2
        assert body["data"]["title"] == "已改"
        # 证伪：HTTP 冲突后库里仍是已改
        assert (await _reload(db, "conflict-1")).title == "已改"

    async def test_board_patch_conflict_returns_409_with_data(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        au = await _au(auth_db, "owner2")
        board = await create_board_ex(db, BoardCreate(slug="olb2", title="板"), au.id)
        await update_board_ex(db, board.id, au.id, BoardUpdate(title="已改", version=1))
        headers = {"Authorization": f"Bearer {au.token}"}

        resp = await client.patch(
            f"/api/v1/content/boards/{board.id}",
            headers=headers,
            json={"title": "过期写", "version": 1},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == CommonErr.VERSION_CONFLICT
        assert body["data"]["version"] == 2
        assert body["data"]["title"] == "已改"
