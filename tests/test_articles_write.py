"""articles 写路径测试：service 层（分类 CRUD、文章写、审核、软/硬删）+ seed 幂等。

覆盖：
- 分类 CRUD：create_category_ex slug 冲突、update_category_ex、delete_category_ex 分类下有文章禁删
- 文章写：draft 初始状态、slug 冲突、分类缺失、编辑分类+状态→published
- 软删保行（status→rejected）；硬删仅 draft 可删、published 拒绝
- 审核仅 pending 可审；pending→published；非 pending 拒绝
- list_categories 从 DB 读回（含各分类文章数）
- 权限：super_admin 收敛（普通/admin 非 super_admin → 403；super_admin → 200）
- seed_categories 幂等且含 engineering；seed_articles 用 category_id 正常
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.modules.admin.models import RolePermission
from app.modules.content.articles.errors import ArticleErr
from app.modules.content.articles.models import Article
from app.modules.content.articles.models import ArticleCategory as ArticleCategoryORM
from app.modules.content.articles.schemas import (
    ArticleCreate,
    ArticleUpdate,
    CategoryCreate,
)
from app.modules.content.articles.seed import seed_articles, seed_categories
from app.modules.content.articles.service import (
    create_article_ex,
    create_category_ex,
    delete_category_ex,
    hard_delete_article,
    list_categories,
    review_article,
    soft_delete_article,
    update_article_ex,
    update_category_ex,
)
from tests.conftest import AuthUser, auth_user_uid

# 合法的 uuid7 形态（第 3 段以 7 开头、第 4 段以 8 开头），用于"不存在"的 id 用例。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _category(db: AsyncSession, slug: str = "news") -> uuid.UUID:
    """新建一个分类，返回分类 id（news / sci / 自定义 slug 均支持缩微定位）。"""
    return (await create_category_ex(db, CategoryCreate(slug=slug, title=slug))).id


async def _article(db: AsyncSession, category_id: uuid.UUID, slug: str = "a1") -> str:
    """以 draft 状态创建一篇归属指定分类的文章，返回 slug。"""
    await create_article_ex(
        db,
        ArticleCreate(
            title="标题",
            slug=slug,
            content="正文",
            category_id=category_id,
            status="draft",
            tags=["数学"],
        ),
    )
    return slug


async def _au(
    auth_db: AsyncSession,
    username: str,
    level: str,
    role: str,
) -> AuthUser:
    """在 auth realm 建一线用户并 mint 对应 (account_level, role) 的 token。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level=level,
        role=role,
    )


class TestArticleWrite:
    """service 层文章写 / 审核 / 删除。"""

    async def test_create_article_draft(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)
        art = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert art is not None
        assert art.status == "draft"
        assert art.published is None
        assert art.category_id == cid

    async def test_create_slug_conflict(self, db: AsyncSession) -> None:
        cid = await _category(db)
        await _article(db, cid)
        with pytest.raises(BizError) as e:
            await _article(db, cid)
        assert e.value.errcode == ArticleErr.SLUG_CONFLICT

    async def test_create_missing_category(self, db: AsyncSession) -> None:
        with pytest.raises(BizError) as e:
            await create_article_ex(
                db,
                ArticleCreate(title="t", slug="x", content="c", category_id=_MISSING_ID),
            )
        assert e.value.errcode == ArticleErr.CATEGORY_NOT_FOUND

    async def test_edit_category_and_status(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)
        cid2 = (
            await create_category_ex(db, CategoryCreate(slug="sci", title="科学"))
        ).id
        await update_article_ex(
            db, slug, ArticleUpdate(category_id=cid2, status="published"), is_super=True
        )
        art = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert art is not None
        assert art.category_id == cid2
        assert art.status == "published"
        assert art.published is not None

    async def test_soft_delete_keeps_rows(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)
        await soft_delete_article(db, slug)
        art = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert art is not None
        assert art.status == "rejected"
        assert art.published is None

    async def test_hard_delete_only_draft(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)  # draft
        await hard_delete_article(db, slug)
        gone = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert gone is None

    async def test_hard_delete_published_rejected(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)
        await update_article_ex(
            db, slug, ArticleUpdate(status="published"), is_super=True
        )
        with pytest.raises(BizError) as e:
            await hard_delete_article(db, slug)
        assert e.value.errcode == ArticleErr.CANNOT_HARD_DELETE_PUBLISHED

    async def test_review_pending_flow(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)
        await update_article_ex(
            db, slug, ArticleUpdate(status="pending"), is_super=True
        )
        await review_article(db, slug, approve=True)
        art = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert (
            art is not None and art.status == "published" and art.published is not None
        )

    async def test_review_reject_flow(self, db: AsyncSession) -> None:
        """审核驳回：pending → rejected，published 保持 None。"""
        cid = await _category(db)
        slug = await _article(db, cid)
        await update_article_ex(
            db, slug, ArticleUpdate(status="pending"), is_super=True
        )
        await review_article(db, slug, approve=False)
        art = (
            (await db.execute(select(Article).where(Article.slug == slug)))
            .scalars()
            .first()
        )
        assert art is not None and art.status == "rejected" and art.published is None

    async def test_review_non_pending_rejected(self, db: AsyncSession) -> None:
        cid = await _category(db)
        slug = await _article(db, cid)  # draft，非 pending
        with pytest.raises(BizError) as e:
            await review_article(db, slug, approve=True)
        assert e.value.errcode == ArticleErr.INVALID_STATUS_TRANSITION

    async def test_list_categories_from_db(self, db: AsyncSession) -> None:
        cid = await _category(db)
        await _article(db, cid)
        cats = await list_categories(db)
        news = next(c for c in cats if c.slug == "news")
        assert news.article_count == 1


class TestArticleCategory:
    """service 层分类 CRUD。"""

    async def test_create_category_slug_conflict(self, db: AsyncSession) -> None:
        await create_category_ex(db, CategoryCreate(slug="tech", title="技术上"))
        with pytest.raises(BizError) as e:
            await create_category_ex(db, CategoryCreate(slug="tech", title="技术下"))
        assert e.value.errcode == ArticleErr.SLUG_CONFLICT

    async def test_update_category(self, db: AsyncSession) -> None:
        cid = await _category(db, slug="news")
        out = await update_category_ex(
            db, cid, CategoryCreate(slug="tech", title="新技术", sort=5)
        )
        assert out.slug == "tech"
        row = (
            (
                await db.execute(
                    select(ArticleCategoryORM).where(ArticleCategoryORM.id == cid)
                )
            )
            .scalars()
            .first()
        )
        assert row is not None and row.title == "新技术" and row.sort == 5

    async def test_update_category_self_no_conflict(self, db: AsyncSession) -> None:
        """更新分类不改 slug 时，自身 slug 不视作冲突。"""
        cid = await _category(db, slug="news")
        out = await update_category_ex(
            db, cid, CategoryCreate(slug="news", title="新闻最新")
        )
        assert out.slug == "news" and out.title == "新闻最新"

    async def test_delete_category_empty(self, db: AsyncSession) -> None:
        cid = await _category(db, slug="news")
        await delete_category_ex(db, cid)
        row = (
            (
                await db.execute(
                    select(ArticleCategoryORM).where(ArticleCategoryORM.id == cid)
                )
            )
            .scalars()
            .first()
        )
        assert row is None

    async def test_delete_category_with_articles_blocked(
        self, db: AsyncSession
    ) -> None:
        cid = await _category(db, slug="news")
        await _article(db, cid)
        with pytest.raises(BizError) as e:
            await delete_category_ex(db, cid)
        assert e.value.errcode == CommonErr.INVALID_INPUT


class TestArticlePermission:
    """写端点 super_admin 收敛（走 client + JWT）。"""

    @pytest.mark.parametrize(
        "level,role",
        [
            ("normal", "member"),  # 普通用户
            ("admin", "editor"),  # admin 但 role 非 super_admin
        ],
    )
    async def test_write_endpoint_forbidden(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        level: str,
        role: str,
    ) -> None:
        """非 super_admin（普通用户或 admin 非 super_admin）调写端点 → 403。"""
        au = await _au(
            auth_db, username=f"u-{role}-{level}", level=level, role=role
        )
        headers = {"Authorization": f"Bearer {au.token}"}
        resp = await client.post(
            "/api/v1/articles",
            headers=headers,
            json={
                "title": "t",
                "slug": "s1",
                "content": "c",
                "category_id": str(_MISSING_ID),
            },
        )
        assert resp.status_code == 403
        assert resp.json().get("code") == CommonErr.FORBIDDEN

    async def test_write_endpoint_super_admin_ok(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        """super_admin（DB 档 account_level=admin + profile.role=super_admin，且授予 articles.publish）→ 创建 200。"""
        # RBAC 迁移后写端点由 RequirePermission(articles.publish) 把关：官方文章仅 super_admin。
        # 测试库 create_all 不自动 seed 权限映射，故在此按生产 DEFAULT_GRANTS 显式补授权。
        db.add(
            RolePermission(role_name="admin:super_admin", permission="articles.publish")
        )
        await db.flush()
        cid = await _category(db, slug="news")
        au = await _au(auth_db, username="root", level="admin", role="super_admin")
        headers = {"Authorization": f"Bearer {au.token}"}
        resp = await client.post(
            "/api/v1/articles",
            headers=headers,
            json={
                "title": "官方发稿",
                "slug": "official-1",
                "content": "正文内容",
                "category_id": str(cid),
                "status": "published",
                "tags": ["官方"],
            },
        )
        assert resp.status_code == 200
        data = resp.json().get("data") or {}
        assert data.get("slug") == "official-1"
        # ArticleDetail schema 不含 status 字段；published 仅在 status=published 时被填充，
        # 故以 published 非空来校验"以发布状态创建"。
        assert data.get("published") is not None

    async def test_write_endpoint_requires_auth(self, client: AsyncClient) -> None:
        """未带 token 调写端点 → 403。"""
        resp = await client.post(
            "/api/v1/articles",
            json={
                "title": "t",
                "slug": "s2",
                "content": "c",
                "category_id": str(_MISSING_ID),
            },
        )
        assert resp.status_code == 403
        assert resp.json().get("code") == CommonErr.FORBIDDEN


class TestArticleSeed:
    """seed_categories / seed_articles。"""

    async def test_seed_categories_idempotent(self, db: AsyncSession) -> None:
        from app.modules.content.articles.seed import _CATEGORIES

        first = await seed_categories(db)
        second = await seed_categories(db)
        assert first == len(_CATEGORIES)
        assert second == 0

    async def test_seed_categories_include_engineering(self, db: AsyncSession) -> None:
        await seed_categories(db)
        rows = (await db.execute(select(ArticleCategoryORM.slug))).scalars().all()
        assert "engineering" in rows

    async def test_seed_articles_with_category_id(self, db: AsyncSession) -> None:
        """seed_articles 依赖已 seed 的分类并解析 category_id；二次幂等为 0。"""
        await seed_categories(db)
        first = await seed_articles(db)
        second = await seed_articles(db)
        assert first > 0
        assert second == 0
        rows = (
            (
                await db.execute(
                    select(ArticleCategoryORM.id).where(
                        ArticleCategoryORM.slug == "news"
                    )
                )
            )
            .scalars()
            .first()
        )
        assert rows is not None
        news_articles = (
            (await db.execute(select(Article).where(Article.category_id == rows)))
            .scalars()
            .all()
        )
        assert len(news_articles) > 0
