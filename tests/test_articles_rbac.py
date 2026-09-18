"""articles 迁移 RBAC：官方文章写/审/删/分类需对应权限点 + auth realm 拆库迁移。

拆库(M3.B S5 dual 真 PG)：users/profiles 迁 auth realm，业务库无 auth 表。任一走 HTTP 鉴权 /
属主判断的用例建用户在 auth realm(auth_db)，注入 auth_db+auth_seam_realm：
- ``_mk_au(auth_db,…)`` 建 auth realm 用户并返回 AuthUser(id/token/role)；
- ``_h(au)`` 以 au.token(headers) 登录，get_current_user/require_* 经 seam 落 auth realm。
- check_owner(ArticleComment.user_id) 用 au.id 断言属主。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import RolePermission
from app.modules.articles.models import Article, ArticleComment
from app.modules.articles.schemas import CategoryCreate
from app.modules.articles.service import create_category_ex
from tests.conftest import DB, AuthUser, Client, auth_user_uid


async def _au(
    auth_db: AsyncSession, username: str, level: str, role: str
) -> AuthUser:
    """在 auth realm 建一线用户并 mint 对应 (account_level, role) 的 token。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@x.com",
        nickname=username[:10],
        account_level=level,
        role=role,
    )


def _h(au: AuthUser, role: str | None = None) -> dict[str, str]:
    # AuthUser.token 已按 (account_level, role) mint；若 test 另行传 role 则重发自定义 curl 太繁，
    # 统一直接用 token（role claim 已含）。保留 role 形参仅为调用点兼容，实际以 token 内 role 为准。
    _ = role
    return {"Authorization": f"Bearer {au.token}"}


async def _grant(db: DB, role_name: str, permission: str) -> None:
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == role_name,
            RolePermission.permission == permission,
        )
    )
    if exists is None:
        db.add(RolePermission(role_name=role_name, permission=permission))
        await db.flush()


async def _category(db: DB) -> uuid.UUID:
    cat = await create_category_ex(db, CategoryCreate(slug="news", title="news"))
    return cat.id


async def test_member_cannot_publish_article(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # normal/member 无 articles.publish → 403（权限点在路由层拦截，早于 service 建文）
    cid = await _category(db)
    u = await _au(auth_db, "member_pub", "normal", "member")
    r = await client.post(
        "/api/v1/articles",
        headers=_h(u),
        json={"slug": "x", "title": "t", "content": "c", "category_id": str(cid)},
    )
    assert r.status_code == 403


async def test_org_member_cannot_review_article(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # admin:org_member 有后台能力但无 articles.review → 审核 403（区分 super_admin 与 org_member）
    u = await _au(auth_db, "org_admin", "admin", "org_member")
    r = await client.post(
        "/api/v1/articles/official-1/review",
        headers=_h(u, role="org_member"),
        json={"approve": True},
    )
    assert r.status_code == 403


async def test_super_admin_can_publish_article(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # super_admin 默认授予 articles.publish；显式补授权以便测试库判权限（create_all 不自动 seed）
    await _grant(db, "admin:super_admin", "articles.publish")
    cid = await _category(db)
    u = await _au(auth_db, "sup", "admin", "super_admin")
    r = await client.post(
        "/api/v1/articles",
        headers=_h(u, role="super_admin"),
        json={
            "slug": "official-1",
            "title": "t",
            "content": "c",
            "category_id": str(cid),
        },
    )
    assert r.status_code == 200
    assert (r.json().get("data") or {}).get("slug") == "official-1"


async def _article(db: DB, category_id: uuid.UUID | None = None) -> uuid.UUID:
    """直插一条 Article，供评论挂在 article_id 下。返回文章 id。"""
    cid = category_id if category_id is not None else await _category(db)
    art = Article(slug="rbac-cmt", title="t", category_id=cid, content="c")
    db.add(art)
    await db.flush()
    return art.id


async def _comment(db: DB, article_id: uuid.UUID, author_id: uuid.UUID) -> uuid.UUID:
    """直插一条评论（评论作者 = author_id），返回评论 id。"""
    cmt = ArticleComment(article_id=article_id, user_id=author_id, content="c")
    db.add(cmt)
    await db.flush()
    return cmt.id


async def test_comment_author_can_delete_own(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 用例1：评论作者（user_id==cur.id）可删自己评论 → 200（走 check_owner 属主判定，无需权限点）
    aid = await _article(db)
    author = await _au(auth_db, "cmt_author", "normal", "member")
    cmt_id = await _comment(db, aid, author.id)
    r = await client.delete(f"/api/v1/articles/comments/{cmt_id}", headers=_h(author))
    assert r.status_code == 200


async def test_other_member_cannot_delete_comment(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 用例2：他人（非评论作者、非 super_admin）删评论 → 403
    aid = await _article(db)
    author = await _au(auth_db, "cmt_author2", "normal", "member")
    cmt_id = await _comment(db, aid, author.id)
    other = await _au(auth_db, "cmt_other", "normal", "member")
    r = await client.delete(f"/api/v1/articles/comments/{cmt_id}", headers=_h(other))
    assert r.status_code == 403


async def test_super_admin_can_delete_any_comment(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 用例3：super_admin 授 article.owner_comment_delete 可代删任意评论（非作者的迁移扩展）→ 200
    await _grant(db, "admin:super_admin", "article.owner_comment_delete")
    aid = await _article(db)
    author = await _au(auth_db, "cmt_author3", "normal", "member")
    cmt_id = await _comment(db, aid, author.id)
    sup = await _au(auth_db, "sup_del", "admin", "super_admin")
    r = await client.delete(
        f"/api/v1/articles/comments/{cmt_id}", headers=_h(sup, role="super_admin")
    )
    assert r.status_code == 200
