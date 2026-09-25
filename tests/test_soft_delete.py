"""批 4：软删除（内容/评论域 4 表新增 + 既有 starhope/feed 6 表统一到 mixin）。

覆盖三类易回归点：

1. **可见性**：软删后列表/详情/检索/收藏/历史/通知解析都不再返回该行，而 ``deleted_at``
   仍留痕（行没被物理删除），``restore`` 可恢复；
2. **口径一致**：计数对账（``content/counters.py``）与在线计数同用「不含已软删」，
   否则两者互相覆盖、永久震荡；
3. **旁路清理**：物化 feed 是写入时快照，不会自动跟随源行软删，必须按 ``source_id`` 清掉。

凡经 service 走作者展示名回填的用例，照 ``tests/test_content.py`` 的既有范式带
``auth_db`` + ``auth_seam_realm``（跨 realm 读缝指向本测 auth 库 schema）。
"""

import datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from app.db.repository import AsyncRepository
from app.modules.content.articles.models import Article, ArticleCategory, ArticleComment
from app.modules.content.articles.repository import ArticleCommentRepository
from app.modules.content.blog.models import BlogSeries
from app.modules.content.blog.repository import BlogCommentRepository
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.counters import reconcile_counts
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentItem
from app.modules.content.repository import (
    ContentCommentRepository,
    ContentItemRepository,
)
from app.modules.content.schemas import ContentCommentCreate, ContentItemCreate
from app.modules.content.service import (
    create_comment,
    create_item,
    delete_item,
    get_item,
)
from app.modules.feed.models import FeedItemMaterialized
from app.modules.interaction.repository import (
    InteractionContentItemRepository,
    InteractionFavoriteRepository,
    InteractionViewLogRepository,
)
from app.modules.interaction.service import add_favorite, list_history, record_view
from app.modules.search.repository import SearchRepository
from tests.conftest import DB, auth_user_uid


async def _au(auth_db: AsyncSession, username: str = "sd-user") -> uuid.UUID:
    user = await auth_user_uid(
        auth_db, username=username, email=f"{username}@example.com"
    )
    return user.id


async def _board(db: AsyncSession, slug: str) -> uuid.UUID:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _item(db: AsyncSession, *, slug: str, author: uuid.UUID, title: str = "标题"):
    return await create_item(
        db,
        author,
        ContentItemCreate(board_id=await _board(db, slug), title=title, content="正文"),
    )


async def _raw_item(db: AsyncSession, item_id: uuid.UUID) -> ContentItem | None:
    """绕过 Repository 的软删过滤直读原行（断言「行还在、只打了墓碑」）。"""
    return await db.scalar(select(ContentItem).where(ContentItem.id == item_id))


# ─────────────────────── 内容项 ───────────────────────


async def test_delete_item_soft_deletes_and_hides(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-a", author=author)
    await delete_item(db, item.id, author)

    row = await _raw_item(db, item.id)
    assert row is not None and row.deleted_at is not None

    with pytest.raises(BizError) as e:
        await get_item(db, item.id)
    assert e.value.errcode == ContentErr.CONTENT_NOT_FOUND

    repo = ContentItemRepository(db)
    assert await repo.get(item.id) is None
    assert await repo.get(item.id, include_deleted=True) is row


async def test_restore_makes_item_visible_again(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-b", author=author)
    repo = ContentItemRepository(db)
    await delete_item(db, item.id, author)
    assert await repo.get(item.id) is None

    tombstone = await repo.get(item.id, include_deleted=True)
    assert tombstone is not None
    await repo.restore(tombstone)
    assert (await get_item(db, item.id)).id == item.id


async def test_slug_uniqueness_counts_tombstones(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """slug 唯一性检查含已软删行：墓碑占位，防删除后同 slug 复活歧义。"""
    author = await _au(auth_db)
    item = await _item(db, slug="sd-c", author=author)
    raw = await _raw_item(db, item.id)
    assert raw is not None
    raw.slug = "tombstone-slug"
    await ContentItemRepository(db).flush()

    await delete_item(db, item.id, author)
    assert await ContentItemRepository(db).slug_taken("tombstone-slug") is True

    with pytest.raises(BizError) as e:
        await create_item(
            db,
            author,
            ContentItemCreate(
                board_id=await _board(db, "sd-c2"),
                title="再来一篇",
                content="x",
                content_type="article",
                slug="tombstone-slug",
            ),
        )
    assert e.value.errcode == ContentErr.SLUG_TAKEN


async def test_search_excludes_soft_deleted(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-search", author=author, title="独门关键词甲乙丙")
    repo = SearchRepository(db)
    assert await repo.count_matching(term="独门关键词甲乙丙") == 1

    await delete_item(db, item.id, author)
    assert await repo.count_matching(term="独门关键词甲乙丙") == 0


async def _materialized_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(FeedItemMaterialized)
            .where(FeedItemMaterialized.user_id == user_id)
        )
        or 0
    )


async def test_feed_materialization_purged_on_delete(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """物化 feed 是写入时快照：软删内容必须显式清掉对应物化行，否则时间线仍可见。"""
    author = await _au(auth_db)
    item = await _item(db, slug="sd-feed", author=author)
    follower = uuid.uuid4()
    db.add(
        FeedItemMaterialized(
            user_id=follower,
            item_type="discussion",
            source_id=item.id,
            author_id=author,
            title="快照标题",
            content_preview="p",
            url=f"/content/posts/{item.id}",
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    await db.flush()
    assert await _materialized_count(db, follower) == 1

    await delete_item(db, item.id, author)
    assert await _materialized_count(db, follower) == 0


# ─────────────────────── 评论与计数口径 ───────────────────────


async def test_comment_floor_skips_soft_deleted(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """楼层号取含已软删的最大值 +1，否则软删后新评论会与可见楼层重号。"""
    author = await _au(auth_db)
    item = await _item(db, slug="sd-floor", author=author)
    first = await create_comment(
        db, item.id, author, ContentCommentCreate(content="一楼")
    )
    repo = ContentCommentRepository(db)
    tombstone = await repo.get(first.id)
    assert tombstone is not None
    await repo.soft_delete(tombstone)

    second = await create_comment(
        db, item.id, author, ContentCommentCreate(content="二楼")
    )
    assert second.floor_number == first.floor_number + 1


async def test_reconcile_ignores_soft_deleted_comments(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """对账真相源与在线计数同口径（不含已软删），否则互相覆盖、永久震荡。"""
    author = await _au(auth_db)
    item = await _item(db, slug="sd-recon", author=author)
    first = await create_comment(db, item.id, author, ContentCommentCreate(content="a"))
    await create_comment(db, item.id, author, ContentCommentCreate(content="b"))

    repo = ContentCommentRepository(db)
    tombstone = await repo.get(first.id)
    assert tombstone is not None
    # 只软删评论、不动计数列（模拟漏减），对账应把它修正回 1
    await repo.soft_delete(tombstone)

    scanned, affected = await reconcile_counts(db)
    assert scanned >= 1
    assert affected == 1
    raw = await _raw_item(db, item.id)
    assert raw is not None and raw.comment_count == 1

    _, affected2 = await reconcile_counts(db)
    assert affected2 == 0  # 可证伪收敛


async def test_comment_list_and_count_exclude_soft_deleted(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-clist", author=author)
    comment = await create_comment(
        db, item.id, author, ContentCommentCreate(content="a")
    )
    repo = ContentCommentRepository(db)
    tombstone = await repo.get(comment.id)
    assert tombstone is not None
    await repo.soft_delete(tombstone)

    assert await repo.count_in_content(item.id) == 0
    assert await repo.list_in_content(item.id) == []
    assert await repo.list_all_in_content(item.id) == []


# ─────────────────────── 文章/博客评论（级联软删） ───────────────────────


async def _article(db: AsyncSession, slug: str) -> Article:
    cat = ArticleCategory(slug=f"cat-{slug}", title="c")
    db.add(cat)
    await db.flush()
    article = Article(slug=slug, title="t", content="c", category_id=cat.id)
    db.add(article)
    await db.flush()
    return article


async def test_article_comment_subtree_soft_deleted(db: DB) -> None:
    article = await _article(db, "sd-art")
    repo = ArticleCommentRepository(db)
    root = await repo.create(article_id=article.id, user_id=uuid.uuid4(), content="根")
    child = await repo.create(
        article_id=article.id,
        user_id=uuid.uuid4(),
        content="回复",
        parent_id=root.id,
    )

    # 与硬删时代的 ORM delete-orphan 级联等价：整棵回复树一起消失
    assert await repo.soft_delete_subtree(root.id) == 2
    assert await repo.list_in_article(article.id) == []

    child_row = await repo.get(child.id, include_deleted=True)
    assert child_row is not None and child_row.deleted_at is not None


async def test_blog_comment_subtree_soft_deleted(db: DB) -> None:
    series = BlogSeries(owner_id=uuid.uuid4(), repo_name="r", title="t")
    db.add(series)
    await db.flush()

    repo = BlogCommentRepository(db)
    root = await repo.create(series_id=series.id, user_id=uuid.uuid4(), content="根")
    await repo.create(
        series_id=series.id, user_id=uuid.uuid4(), content="回复", parent_id=root.id
    )

    assert await repo.soft_delete_subtree(root.id) == 2
    assert await repo.list_in_series(series.id) == []


async def test_article_comment_repo_get_filters_tombstone(db: DB) -> None:
    article = await _article(db, "sd-art2")
    repo = ArticleCommentRepository(db)
    comment = await repo.create(
        article_id=article.id, user_id=uuid.uuid4(), content="x"
    )
    plain = await repo.get(comment.id)
    assert plain is not None
    await repo.soft_delete(plain)

    assert await repo.get(comment.id) is None
    assert isinstance(await repo.get(comment.id, include_deleted=True), ArticleComment)


# ─────────────────────── 跨模块读缝 ───────────────────────


async def test_interaction_hides_soft_deleted_content(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-fav", author=author)
    user_id = uuid.uuid4()
    await add_favorite(db, user_id, item.id)
    assert await InteractionContentItemRepository(db).exists_content(item.id) is True

    await delete_item(db, item.id, author)

    # 浏览上报/收藏的 404 前置判定：软删内容视同不存在
    assert await InteractionContentItemRepository(db).exists_content(item.id) is False
    page = await InteractionFavoriteRepository(db).list_page(
        user_id, offset=0, limit=20
    )
    assert page == []


async def test_history_hides_soft_deleted_content(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db)
    item = await _item(db, slug="sd-hist", author=author)
    user_id = uuid.uuid4()
    await record_view(db, user_id, item.id)
    assert (await list_history(db, user_id)).total == 1

    await delete_item(db, item.id, author)

    # 浏览明细行仍在（历史是用户侧数据，只影响展示），但列表与 count 同口径都不再展示
    assert await InteractionViewLogRepository(db).count_for_user(user_id) == 0
    assert (await list_history(db, user_id)).total == 0
    from app.modules.interaction.models import InteractionViewLog

    assert (
        await db.scalar(
            select(func.count())
            .select_from(InteractionViewLog)
            .where(InteractionViewLog.user_id == user_id)
        )
        == 1
    )


async def test_notification_target_resolution_skips_soft_deleted(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """事件可能晚于删除到达：解析目标时按已删过滤，不再产生新通知。"""
    from app.modules.notification.tasks import _resolve_targets

    author = await _au(auth_db)
    item = await _item(db, slug="sd-notif", author=author)
    await delete_item(db, item.id, author)
    assert await _resolve_targets(db, "like", f"item:{item.id}") == []


async def test_starhope_mixin_round_trip(db: DB) -> None:
    """既有 6 表统一到 SoftDeleteMixin 后行为不变（列定义逐字一致 → schema 零 diff）。"""
    from app.modules.starhope.models import StarHopeFolder

    class FolderRepo(AsyncRepository[StarHopeFolder]):
        model = StarHopeFolder

    repo = FolderRepo(db)
    user_id = uuid.uuid4()
    folder = await repo.create(id=str(uuid.uuid4()), user_id=user_id, name="f")
    assert repo.soft_delete_column is not None

    await repo.soft_delete(folder)
    assert await repo.count(StarHopeFolder.user_id == user_id) == 0
    assert (
        await repo.count(StarHopeFolder.user_id == user_id, include_deleted=True) == 1
    )


async def test_restore_is_noop_for_model_without_column(db: DB) -> None:
    """无 ``deleted_at`` 列的模型上 restore 是空操作（批 3 基类的动态判定契约）。"""
    from app.modules.content.articles.models import ArticleCategory

    class CategoryRepo(AsyncRepository[ArticleCategory]):
        model = ArticleCategory

    repo = CategoryRepo(db)
    cat = await repo.create(slug="sd-noop", title="c")
    assert repo.soft_delete_column is None
    await repo.restore(cat)  # 不抛
    assert await repo.get(cat.id) is cat


async def test_soft_deleted_item_comment_creation_rejected(
    db: DB, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """已软删内容视同不存在：不能再对其发评论。"""
    author = await _au(auth_db)
    item = await _item(db, slug="sd-cmt", author=author)
    await delete_item(db, item.id, author)

    with pytest.raises(BizError) as e:
        await create_comment(db, item.id, author, ContentCommentCreate(content="x"))
    assert e.value.errcode == ContentErr.CONTENT_NOT_FOUND
    assert e.value.errcode != CommonErr.INVALID_INPUT
