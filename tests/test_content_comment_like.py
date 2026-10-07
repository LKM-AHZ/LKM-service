"""评论点赞（content_comment_likes）与评论删除：幂等、计数、越界、软删、权限点。

拆库后业务库(Base 无 users)不再有 User/Profile：content_comments.user_id 是 auth realm
稳定裸 uuid。凡需作者身份/展示名回填的用例注入 ``auth_db`` + ``auth_seam_realm``。

覆盖：
- 评论点赞幂等 / 取消幂等 / 多用户各自计数
- item_id 与 comment_id 不配套 → COMMENT_NOT_FOUND（计数不会记到别的帖子上）
- 已软删评论点赞 → COMMENT_NOT_FOUND
- 删除评论：软删 + 帖子 comment_count 递减 + 重复删除 404
- 列表按 viewer 回填 liked
- 对账把评论 like_count 拉回明细真值
- HTTP：点赞路由挂了 content.like 权限点（local 账户 403）；评论列表路由分页与 liked
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import RolePermission
from app.modules.content import graphql as content_graphql
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.counters import (
    reconcile_counts,
    reset_reconcile_oscillation_state,
)
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentComment
from app.modules.content.repository import ContentCommentRepository
from app.modules.content.schemas import ContentCommentCreate, ContentItemCreate
from app.modules.content.service import (
    create_comment,
    create_item,
    delete_comment,
    get_item,
    like_comment,
    like_item,
    list_comments,
    unlike_comment,
)
from app.modules.interaction import graphql as interaction_graphql
from app.modules.interaction import service as interaction_service
from app.modules.interaction.errors import InteractionErr
from core.common import PageData
from core.err import BizError
from tests.conftest import DB, AuthUser, Client, auth_user_uid


async def _au(
    auth_db: AsyncSession, username: str = "alice", account_level: str = "normal"
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser（裸 .id 作业务 user_id）。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level=account_level,
    )


async def _make_item(db: AsyncSession, uid: uuid.UUID, slug: str = "b") -> uuid.UUID:
    board_id = (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id
    item = await create_item(
        db, uid, ContentItemCreate(board_id=board_id, title="t", content="c")
    )
    return item.id


async def _make_comment(
    db: AsyncSession, item_id: uuid.UUID, uid: uuid.UUID, text: str = "一楼"
) -> uuid.UUID:
    return (await create_comment(db, item_id, uid, ContentCommentCreate(content=text))).id


async def test_comment_like_idempotent_and_unlike(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "clk")).id
    item_id = await _make_item(db, uid)
    cid = await _make_comment(db, item_id, uid)

    assert await like_comment(db, item_id, cid, uid) == 1
    assert await like_comment(db, item_id, cid, uid) == 1  # 幂等：不重复计数
    assert await unlike_comment(db, item_id, cid, uid) == 0
    assert await unlike_comment(db, item_id, cid, uid) == 0  # 取消幂等：不减成负数


async def test_comment_like_counts_per_user(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    a = (await _au(auth_db, "ca")).id
    b = (await _au(auth_db, "cb")).id
    item_id = await _make_item(db, a)
    cid = await _make_comment(db, item_id, a)

    assert await like_comment(db, item_id, cid, a) == 1
    assert await like_comment(db, item_id, cid, b) == 2
    # 帖子本身的点赞计数不受评论点赞影响（两个字段是两套明细）
    assert (await get_item(db, item_id)).like_count == 0


async def test_comment_like_rejects_mismatched_item(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """item_id 与 comment_id 不配套时必须 404，否则计数会记到别的帖子上。"""
    uid = (await _au(auth_db, "cm")).id
    item_a = await _make_item(db, uid, slug="ba")
    item_b = await _make_item(db, uid, slug="bb")
    cid = await _make_comment(db, item_a, uid)

    with pytest.raises(BizError) as exc:
        await like_comment(db, item_b, cid, uid)
    assert exc.value.errcode == ContentErr.COMMENT_NOT_FOUND


async def test_comment_like_soft_deleted_comment_is_404(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "cs")).id
    item_id = await _make_item(db, uid)
    cid = await _make_comment(db, item_id, uid)
    comment = await ContentCommentRepository(db).get_one_or_raise(
        ContentErr.COMMENT_NOT_FOUND, ContentComment.id == cid
    )
    await ContentCommentRepository(db).soft_delete(comment)

    with pytest.raises(BizError) as exc:
        await like_comment(db, item_id, cid, uid)
    assert exc.value.errcode == ContentErr.COMMENT_NOT_FOUND


async def test_delete_comment_soft_deletes_and_decrements_count(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "cd")).id
    item_id = await _make_item(db, uid)
    cid = await _make_comment(db, item_id, uid)
    assert (await get_item(db, item_id)).comment_count == 1

    await delete_comment(db, item_id, cid)

    assert (await get_item(db, item_id)).comment_count == 0
    assert (await list_comments(db, item_id)).total == 0
    # 已软删的评论不能再删一次（软删过滤生效，而非重复递减计数）
    with pytest.raises(BizError) as exc:
        await delete_comment(db, item_id, cid)
    assert exc.value.errcode == ContentErr.COMMENT_NOT_FOUND
    assert (await get_item(db, item_id)).comment_count == 0


async def test_delete_comment_keeps_floor_number_monotonic(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """软删后新评论不得复用可见楼层号（楼层取含软删的最大值 +1）。"""
    uid = (await _au(auth_db, "cf")).id
    item_id = await _make_item(db, uid)
    cid = await _make_comment(db, item_id, uid, "一楼")
    await delete_comment(db, item_id, cid)

    second = await create_comment(
        db, item_id, uid, ContentCommentCreate(content="二楼")
    )
    assert second.floor_number == 2


async def test_list_comments_marks_viewer_liked(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    a = (await _au(auth_db, "va")).id
    b = (await _au(auth_db, "vb")).id
    item_id = await _make_item(db, a)
    cid = await _make_comment(db, item_id, a)
    await like_comment(db, item_id, cid, a)

    mine = await list_comments(db, item_id, viewer_id=a)
    assert mine.items[0].liked is True
    other = await list_comments(db, item_id, viewer_id=b)
    assert other.items[0].liked is False
    # 未指定 viewer（匿名读口）：不回填，不查明细表
    assert (await list_comments(db, item_id)).items[0].liked is False


async def test_reconcile_repairs_comment_like_count(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """评论 like_count 是派生列：被改坏后由对账按明细拉回真值。"""
    uid = (await _au(auth_db, "cr")).id
    item_id = await _make_item(db, uid)
    cid = await _make_comment(db, item_id, uid)
    await like_comment(db, item_id, cid, uid)

    await db.execute(
        sa_update(ContentComment)
        .where(ContentComment.id == cid)
        .values(like_count=99)
    )
    reset_reconcile_oscillation_state()
    await reconcile_counts(db, only_unconverged=False)

    assert await db.scalar(
        select(ContentComment.like_count).where(ContentComment.id == cid)
    ) == 1


async def test_viewer_state_anonymous_returns_counts_only(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """匿名访客也要看到正确计数，但 liked/favorited 不能凭空为真。"""
    uid = (await _au(auth_db, "anon")).id
    item_id = await _make_item(db, uid)
    await like_item(db, item_id, uid)

    liked, favorited, like_count, bookmark_count = (
        await interaction_service.get_content_viewer_state(db, None, item_id)
    )

    assert (liked, favorited) == (False, False)
    assert like_count == 1 and bookmark_count == 0

    # 已登录时同一读口回真实互动态
    mine = await interaction_service.get_content_viewer_state(db, uid, item_id)
    assert mine[0] is True


async def test_viewer_state_missing_item_is_404(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "vmissing")).id
    with pytest.raises(BizError) as exc:
        await interaction_service.get_content_viewer_state(db, uid, uuid.uuid4())
    assert exc.value.errcode == InteractionErr.CONTENT_NOT_FOUND


async def _seed_perm(db: DB, role: str, permission: str) -> None:
    db.add(RolePermission(role_name=role, permission=permission))
    await db.flush()


def _h(au: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {au.token}"}


async def test_http_local_cannot_like_item(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """点赞路由此前漏挂权限点：local 账户点得动却不落库，现在必须 403。"""
    await _seed_perm(db, "normal:member", "content.like")
    normal = await _au(auth_db, "ok_like")
    local = await _au(auth_db, "local_like", account_level="local")
    item_id = await _make_item(db, normal.id)

    r_local = await client.post(
        f"/api/v1/content/items/{item_id}/like", headers=_h(local)
    )
    assert r_local.status_code == 403

    r_ok = await client.post(
        f"/api/v1/content/items/{item_id}/like", headers=_h(normal)
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["data"]["like_count"] == 1


async def test_http_comment_like_and_list(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.like")
    await _seed_perm(db, "normal:member", "content.comment_create")
    user = await _au(auth_db, "hc")
    item_id = await _make_item(db, user.id)
    cid = await _make_comment(db, item_id, user.id)

    r = await client.post(
        f"/api/v1/content/items/{item_id}/comments/{cid}/like", headers=_h(user)
    )
    assert r.status_code == 200
    assert r.json()["data"]["like_count"] == 1

    r_list = await client.get(
        f"/api/v1/content/items/{item_id}/comments", headers=_h(user)
    )
    assert r_list.status_code == 200
    body = r_list.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["liked"] is True
    assert body["items"][0]["like_count"] == 1

    r_unlike = await client.delete(
        f"/api/v1/content/items/{item_id}/comments/{cid}/like", headers=_h(user)
    )
    assert r_unlike.status_code == 200
    assert r_unlike.json()["data"]["like_count"] == 0


@pytest.mark.asyncio
async def test_graphql_content_viewer_state_converts_id_and_passes_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """contentViewerState 是详情页按钮初值的唯一来源，ID 转换与 viewer 透传都要锁住。"""
    item_id = uuid.uuid4()
    uid = uuid.uuid4()
    seen: dict[str, object] = {}

    async def fake_state(
        _db: object, user_id: uuid.UUID | None, content_id: uuid.UUID
    ) -> tuple[bool, bool, int, int]:
        seen.update(user_id=user_id, content_id=content_id)
        return True, False, 3, 0

    monkeypatch.setattr(
        interaction_graphql.interaction_service,
        "get_content_viewer_state",
        fake_state,
    )
    info = SimpleNamespace(context=SimpleNamespace(db=object(), user_id=uid))

    state = await interaction_graphql.ContentViewerQuery().contentViewerState(
        info, str(item_id)
    )

    # GraphQL ID 是字符串，落到 service 必须是 uuid，否则查询按字符串比对会全空
    assert seen == {"user_id": uid, "content_id": item_id}
    assert (state.liked, state.favorited) == (True, False)
    assert (state.likeCount, state.bookmarkCount) == (3, 0)


@pytest.mark.asyncio
async def test_graphql_comments_pass_viewer_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评论列表要带上请求者，否则每条的 liked 恒 false（前端渲染成「全部未赞」）。"""
    item_id = uuid.uuid4()
    uid = uuid.uuid4()
    seen: dict[str, object] = {}

    async def fake_list(
        _db: object,
        item_id: uuid.UUID,
        *,
        page: int,
        limit: int,
        viewer_id: uuid.UUID | None = None,
    ) -> PageData:
        seen.update(item_id=item_id, viewer_id=viewer_id, page=page, limit=limit)
        return PageData(items=[], total=0, page=page, pages=0)

    monkeypatch.setattr(content_graphql, "list_comments", fake_list)
    info = SimpleNamespace(context=SimpleNamespace(db=object(), user_id=uid))

    await content_graphql.ContentQuery().contentComments(
        info, itemId=str(item_id), page=2, pageSize=5
    )

    assert seen == {"item_id": item_id, "viewer_id": uid, "page": 2, "limit": 5}


async def test_http_foreign_cannot_delete_comment(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.comment_create")
    owner = await _au(auth_db, "cowner")
    other = await _au(auth_db, "cother")
    item_id = await _make_item(db, owner.id)
    cid = await _make_comment(db, item_id, owner.id)

    r = await client.delete(
        f"/api/v1/content/items/{item_id}/comments/{cid}", headers=_h(other)
    )
    assert r.status_code == 403

    r_own = await client.delete(
        f"/api/v1/content/items/{item_id}/comments/{cid}", headers=_h(owner)
    )
    assert r_own.status_code == 200
