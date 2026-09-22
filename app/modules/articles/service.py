import uuid
from datetime import datetime
from typing import Any

from app.core.cache import (
    TTL_ITEM_S,
    TTL_LIST_S,
    bump_collection_version,
    cache_invalidate,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import PageData, paginate_pages
from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.db.repo import get_or_raise
from app.db.repository import DbSession
from app.modules.articles.errors import ArticleErr
from app.modules.articles.models import (
    Article,
    ArticleComment,
)
from app.modules.articles.models import (
    ArticleCategory as ArticleCategoryORM,
)
from app.modules.articles.repository import (
    ArticleCategoryRepository,
    ArticleCommentRepository,
    ArticleLikeRepository,
    ArticleRepository,
    ArticleTagRepository,
)
from app.modules.articles.schemas import (
    ArticleCategory,
    ArticleCommentOut,
    ArticleCreate,
    ArticleDetail,
    ArticleListItem,
    ArticleUpdate,
    CategoryCreate,
    CategoryOut,
)
from app.modules.points.rules import enqueue_points_event
from auth.schemas import ProfileInfo
from auth.snapshot import (
    get_user_snapshot_batch,
    profile_info_from_snap,
)

# 默认阅读速度：中文约 300 字/分钟
READING_SPEED_CPS = 300


def estimate_reading_time(content: str) -> int:
    """按中文字符数估算阅读分钟数（不足 1 分钟计 1；空内容为 0）。"""
    text_length = len(content)
    if not text_length:
        return 0
    return max(1, round(text_length / READING_SPEED_CPS))


async def _invalidate_article_cache(db: DbSession, slug: str) -> None:
    """文章写后使列表/分类/单篇缓存失效，保证写后读一致。

    集合列表用版本号失效（免 SCAN）；单篇与分类按具体键删除。
    *db* 参数仅为调用侧语义一致（本身无需连接）。
    """
    await bump_collection_version("articles")
    await cache_invalidate(
        make_key("articles:by_slug", slug),
        make_key("articles:categories", "ver"),
    )


async def _sync_article_tags(
    db: DbSession, article_id: uuid.UUID, names: list[str]
) -> None:
    """按 name upsert Tag 并关联 ArticleTag（幂等，批量 O(log N)，保序去重）。

    实现已下沉至 :meth:`ArticleTagRepository.sync_for_article`（SQLAlchemy 不过
    service 层）；此处保留原私有函数名与调用契约。
    """
    await ArticleTagRepository(db).sync_for_article(article_id, names)


async def create_article(
    db: DbSession,
    slug: str,
    title: str,
    category: str,
    content: str,
    published: datetime | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Article:
    # 幂等：同 slug 已存在则更新（重发 = 更新）
    repo = ArticleRepository(db)
    existing = await repo.get_by_slug(slug)
    if existing:
        existing.title = title
        existing.category_id = await _resolve_category_id(db, category)
        existing.content = content
        if description is not None:
            existing.description = description
        await _sync_article_tags(db, existing.id, tags or [])
        existing.updated_at = now_iso()
        await repo.flush()
        await _invalidate_article_cache(db, slug)
        return existing
    article = Article(
        slug=slug,
        title=title,
        category_id=await _resolve_category_id(db, category),
        content=content,
        published=published or now_iso(),
        description=description,
    )
    await repo.add(article)
    if tags:
        await _sync_article_tags(db, article.id, tags)
    await _invalidate_article_cache(db, slug)
    return article


async def list_articles(
    db: DbSession, page: int = 1, limit: int = 50
) -> PageData[ArticleListItem]:
    ver = await collection_version("articles")

    async def _load() -> dict[str, Any]:
        repo = ArticleRepository(db)
        total = await repo.count()
        items = await repo.list_page(offset=(page - 1) * limit, limit=limit)
        return {
            "items": [ArticleListItem.model_validate(a).model_dump() for a in items],
            "total": total,
            "page": page,
            "pages": paginate_pages(total, limit),
        }

    payload = await cached_read(
        make_key("articles:list", ver, page, limit), TTL_LIST_S, _load
    )
    return PageData[ArticleListItem].model_validate(payload)


async def get_article(db: DbSession, slug: str) -> ArticleDetail:
    async def _load() -> dict[str, Any]:
        article = await get_or_raise(
            db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
        )
        # article.tags 是 Tag 对象列表，ArticleDetail.tags 期望字符串 list。
        # 不能直接 model_validate(article)：from_attributes 会读 article.tags 得到
        # Tag 对象而校验失败，故从标量属性构造 dict，tags 单独 map 成字符串。
        detail = ArticleDetail(
            **{
                k: v
                for k, v in article.__dict__.items()
                if k in ArticleDetail.model_fields and k != "tags"
            },
            tags=[t.name for t in (article.tags or [])],
        )
        # 与 _article_to_detail 对齐：不填 category_title 会留下 schema 默认空串，
        # 详情查询（GraphQL article.categoryTitle）恒报空分类
        detail.category_title = await _load_category_title(db, article.category_id)
        detail.reading_time = estimate_reading_time(article.content)
        return detail.model_dump()

    payload = await cached_read(make_key("articles:by_slug", slug), TTL_ITEM_S, _load)
    return ArticleDetail.model_validate(payload)


async def list_categories(db: DbSession) -> list[ArticleCategory]:
    """分类列表：读 article_categories 表 + 各分类文章数，缓存。返回 schema（slug/name/count）。"""

    async def _load() -> list[dict[str, Any]]:
        rows = await ArticleCategoryRepository(db).list_with_article_counts()
        return [
            ArticleCategory(
                slug=cat.slug,
                name=cat.title,
                article_count=count,
            ).model_dump()
            for cat, count in rows
        ]

    payload = await cached_read(
        make_key("articles:categories", "ver"), TTL_LIST_S, _load
    )
    return [ArticleCategory.model_validate(p) for p in payload]


async def search_articles(
    db: DbSession, q: str, page: int = 1, limit: int = 50
) -> PageData[ArticleListItem]:
    items, total = await ArticleRepository(db).list_for_search(
        q, offset=(page - 1) * limit, limit=limit
    )
    return PageData(
        items=[ArticleListItem.model_validate(a) for a in items],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def list_tags(db: DbSession) -> list[dict[str, Any]]:
    rows = await ArticleTagRepository(db).list_tag_counts()
    return [{"name": name, "article_count": count} for name, count in rows]


async def get_about() -> dict[str, str]:
    return {
        "title": "LKM 官方博客",
        "description": "LKM 团队博客，发布技术文章与官方资讯。",
        "maintainer": "LKM",
    }


async def _bump_article_count(
    db: DbSession, article_id: uuid.UUID, column: str, delta: int
) -> None:
    """原子回填计数列（SET col = col ± N），防并发丢更新。"""
    await ArticleRepository(db).bump_count(article_id, column, delta)


async def toggle_article_like(
    db: DbSession, slug: str, user_id: uuid.UUID
) -> dict[str, Any]:
    article = await get_or_raise(
        db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
    )
    repo = ArticleLikeRepository(db)
    existing = await repo.get_one_like(article_id=article.id, user_id=user_id)
    if existing:
        # 原子删除：并发两次「取消」只有一个真删到行，不会重复 -1
        if await repo.release_like(article_id=article.id, user_id=user_id):
            await _bump_article_count(db, article.id, "likes", -1)
        liked = False
    else:
        # 原子占位：并发两次「点赞」只有一个真插入（原先后到者撞复合主键报错，且两边
        # 都 +1、都入队积分事件）
        if await repo.claim_like(article_id=article.id, user_id=user_id):
            await _bump_article_count(db, article.id, "likes", 1)
            # 仅新增点赞路径入队（取消点赞不重复计分）
            await enqueue_points_event(db, user_id, "like", f"article:{article.id}")
        liked = True
    # 计数列已变，而详情/列表缓存里内嵌了 likes/comments：不失效会让详情页在 TTL（300s）
    # 内一直显示旧点赞数（点赞瞬间数字跳回去）。两条路径（点赞/取消）都要失效。
    await _invalidate_article_cache(db, slug)
    like_count = await repo.count_for(article.id)
    return {"liked": liked, "like_count": like_count}


async def create_article_comment(
    db: DbSession,
    slug: str,
    user_id: uuid.UUID,
    content: str,
    parent_id: uuid.UUID | None = None,
) -> ArticleComment:
    article = await get_or_raise(
        db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
    )
    if parent_id is not None:
        parent = await get_or_raise(
            db,
            ArticleComment,
            ArticleErr.COMMENT_NOT_FOUND,
            ArticleComment.id == parent_id,
        )
        if parent.article_id != article.id:
            raise BizError(ArticleErr.COMMENT_PARENT_MISMATCH)
    comment = ArticleComment(
        article_id=article.id, user_id=user_id, content=content, parent_id=parent_id
    )
    await ArticleCommentRepository(db).add(comment)
    await _bump_article_count(db, article.id, "comments", 1)
    # 同 toggle_article_like：详情/列表缓存内嵌 comments 计数，写后必须失效
    await _invalidate_article_cache(db, slug)
    return comment


async def _get_author_profiles(
    db: DbSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProfileInfo | None]:
    """批量取评论作者 ProfileInfo（M3.A残项：经 auth 批量读缝一次查齐，不再直读 Profile）。"""
    if not user_ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(user_ids))
    return {uid: profile_info_from_snap(snaps[uid]) for uid in snaps}


async def list_article_comments(db: DbSession, slug: str) -> list[ArticleCommentOut]:
    article = await get_or_raise(
        db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
    )
    rows = await ArticleCommentRepository(db).list_in_article(article.id)
    user_ids = {c.user_id for c in rows}
    profiles = await _get_author_profiles(db, user_ids)
    return [
        ArticleCommentOut.model_validate(c).model_copy(
            update={"profile": profiles.get(c.user_id)}
        )
        for c in rows
    ]


async def delete_article_comment(
    db: DbSession,
    comment_id: uuid.UUID,
    user_id: uuid.UUID,
    as_admin: bool = False,
) -> uuid.UUID:
    comment = await get_or_raise(
        db,
        ArticleComment,
        ArticleErr.COMMENT_NOT_FOUND,
        ArticleComment.id == comment_id,
    )
    if not as_admin and comment.user_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    author_id = comment.user_id
    # 批 4：改软删（行保留以便恢复）；评论列表经 Repository 基类自动过滤已软删。
    # 连带整棵回复树（等价硬删时代的 ORM delete-orphan 级联），故 comments 计数按
    # **实际消失的行数**递减——旧实现恒 -1，与级联删除的行数不一致（既有偏差，顺带修正）。
    removed = await ArticleCommentRepository(db).soft_delete_subtree(comment.id)
    await _bump_article_count(db, comment.article_id, "comments", -removed)
    # 详情缓存按 slug 建键，而本函数只拿到 comment.article_id，故需回查 slug 才能失效
    # （article 行理论上必在；硬删后取不到就跳过，缓存自然随 TTL 过期）
    article = await ArticleRepository(db).get(comment.article_id)
    if article is not None:
        await _invalidate_article_cache(db, article.slug)
    return author_id


# ————— 分类 CRUD（写操作走 service，读列表复用 list_categories） —————


async def _invalidate_categories_cache() -> None:
    """分类变更后使分类列表缓存失效（单键删除，集合版本由 _invalidate_article_cache 负责）。"""
    await cache_invalidate(make_key("articles:categories", "ver"))


async def create_category_ex(db: DbSession, info: CategoryCreate) -> CategoryOut:
    """新建分类；slug 冲突抛出 409。"""
    repo = ArticleCategoryRepository(db)
    if await repo.slug_taken(info.slug):
        raise BizError(ArticleErr.SLUG_CONFLICT)
    cat = ArticleCategoryORM(slug=info.slug, title=info.title, sort=info.sort)
    await repo.add(cat)
    await _invalidate_categories_cache()
    return CategoryOut.model_validate(cat)


async def update_category_ex(
    db: DbSession, category_id: uuid.UUID, patch: CategoryCreate
) -> CategoryOut:
    """更新分类；slug 冲突（排除自身）抛出 409。"""
    cat = await get_or_raise(
        db,
        ArticleCategoryORM,
        ArticleErr.CATEGORY_NOT_FOUND,
        ArticleCategoryORM.id == category_id,
    )
    repo = ArticleCategoryRepository(db)
    if await repo.slug_taken(patch.slug, exclude_id=category_id):
        raise BizError(ArticleErr.SLUG_CONFLICT)
    cat.slug = patch.slug
    cat.title = patch.title
    cat.sort = patch.sort
    await repo.flush()
    await _invalidate_categories_cache()
    return CategoryOut.model_validate(cat)


async def delete_category_ex(db: DbSession, category_id: uuid.UUID) -> None:
    """删除分类；分类下仍有文章时禁止删除。"""
    cat = await get_or_raise(
        db,
        ArticleCategoryORM,
        ArticleErr.CATEGORY_NOT_FOUND,
        ArticleCategoryORM.id == category_id,
    )
    used = await ArticleRepository(db).count_in_category(category_id)
    if used:
        raise BizError(CommonErr.INVALID_INPUT, "分类下仍有文章，不可删除")
    await ArticleCategoryRepository(db).delete(cat)
    await _invalidate_categories_cache()


async def _resolve_category_id(db: DbSession, slug: str) -> uuid.UUID:
    """按 slug 解析分类 id（旧 blog/seed 流程传 slug，这里保向兼容）；不存在则 404。"""
    category_id = await ArticleCategoryRepository(db).id_by_slug(slug)
    if category_id is None:
        raise BizError(ArticleErr.CATEGORY_NOT_FOUND)
    return category_id


# ————— 文章写接口 / 删除 / 审核 —————


async def _require_category(db: DbSession, category_id: uuid.UUID) -> None:
    """校验分类存在，否则抛出 404。"""
    if not await ArticleCategoryRepository(db).id_exists(category_id):
        raise BizError(ArticleErr.CATEGORY_NOT_FOUND)


async def _get_article(db: DbSession, slug: str) -> Article:
    """按 slug 取文章，不存在则抛出 404。"""
    return await get_or_raise(db, Article, ArticleErr.NOT_FOUND, Article.slug == slug)


async def _load_category_title(db: DbSession, category_id: uuid.UUID) -> str:
    """一次查询分类 title，供详情填充 category_title。"""
    title = await ArticleCategoryRepository(db).title_by_id(category_id)
    return str(title) if title is not None else ""


async def _article_to_detail(db: DbSession, article: Article) -> ArticleDetail:
    """把 Article ORM 组装为 ArticleDetail，填充 category_title 与阅读时长。"""
    # article.tags 是 lazy="selectin" 的异步关系：调用方常以刚 flush/新创建
    # 的 Article 传入（tags 未预载）。若在此同步访问 article.tags 会在 async
    # 会话中触发懒加载而抛 MissingGreenlet，故先显式 refresh 按需加载该关系。
    await ArticleRepository(db).refresh_tags(article)
    detail = ArticleDetail(
        **{
            k: v
            for k, v in article.__dict__.items()
            if k in ArticleDetail.model_fields and k != "tags"
        },
        tags=[t.name for t in (article.tags or [])],
    )
    detail.category_title = await _load_category_title(db, article.category_id)
    detail.reading_time = estimate_reading_time(article.content or "")
    return detail


async def create_article_ex(db: DbSession, info: ArticleCreate) -> ArticleDetail:
    """创建文章：slug 冲突与分类存在性校验；status=published 即填充发布时间。"""
    repo = ArticleRepository(db)
    if await repo.slug_taken(info.slug):
        raise BizError(ArticleErr.SLUG_CONFLICT)
    await _require_category(db, info.category_id)
    article = Article(
        slug=info.slug,
        title=info.title,
        description=info.description,
        cover=info.cover,
        content=info.content,
        category_id=info.category_id,
        keywords=",".join(k.strip() for k in info.keywords if k.strip()),
        department=info.department,
        publisher=info.publisher,
        status=info.status,
        published=now_iso() if info.status == "published" else None,
    )
    await repo.add(article)
    await _sync_article_tags(db, article.id, info.tags)
    await _invalidate_article_cache(db, info.slug)
    return await _article_to_detail(db, article)


async def update_article_ex(
    db: DbSession, slug: str, patch: ArticleUpdate, is_super: bool
) -> ArticleDetail:
    """更新文章（仅更新传入字段）。is_super 预留审核/越权语义（当前未用，接口契约保留）。"""
    article = await _get_article(db, slug)
    data = patch.model_dump(exclude_unset=True)
    if data.get("category_id") is not None:
        await _require_category(db, data["category_id"])
    if "status" in data:
        article.status = str(data["status"])
        if data["status"] == "published" and article.published is None:
            article.published = now_iso()
        data.pop("status")
    if "keyword_str" in data:
        article.keywords = str(data["keyword_str"])
        data.pop("keyword_str")
    # tags 不能走通用 setattr：Article.tags 是 list[Tag] 关系列，赋 list[str] 会污染
    # 关系状态并在 flush 时炸；标签只由下面的 _sync_article_tags 走仓储维护
    data.pop("tags", None)
    for k, v in data.items():
        setattr(article, k, v)
    if patch.tags is not None:
        await _sync_article_tags(db, article.id, patch.tags)
    await ArticleRepository(db).flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)


async def soft_delete_article(db: DbSession, slug: str) -> ArticleDetail:
    """软删：status 置为 rejected，清空 published。"""
    article = await _get_article(db, slug)
    article.status = "rejected"
    article.published = None
    await ArticleRepository(db).flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)


async def hard_delete_article(db: DbSession, slug: str) -> None:
    """硬删：published/pending 状态的文章禁止硬删（避免已展示/待审内容被直接破坏）。"""
    article = await _get_article(db, slug)
    if article.status in ("published", "pending"):
        raise BizError(ArticleErr.CANNOT_HARD_DELETE_PUBLISHED)
    # 级联删关联。article_tag/article_comments/article_likes 对 article 的外键在模型层
    # 声明 ondelete="CASCADE"，交给数据库在删 article 时级联清子行。ORM 的
    # cascade/delete-orphan 只对挂进 relationship 集合的对象生效，而本服务以
    # ``db.add(<独立关联对象>)`` 落盘子行，追不到——若不加 DB 级 CASCADE，PG 下
    # DELETE articles 会被 NO ACTION 外键拦截、遗下孤儿。
    await ArticleRepository(db).delete(article)
    await _invalidate_article_cache(db, slug)


async def review_article(db: DbSession, slug: str, approve: bool) -> ArticleDetail:
    """审核：仅 pending 可审；approve→published（填发布时间），否则 rejected。"""
    article = await _get_article(db, slug)
    if article.status != "pending":
        raise BizError(ArticleErr.INVALID_STATUS_TRANSITION)
    article.status = "published" if approve else "rejected"
    if approve:
        article.published = now_iso()
    await ArticleRepository(db).flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)
