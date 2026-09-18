import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    TTL_ITEM_S,
    TTL_LIST_S,
    bump_collection_version,
    cache_invalidate,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import PageData, paginate_pages, tag_names_sequence
from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.db.repo import get_or_raise
from app.modules.articles.errors import ArticleErr
from app.modules.articles.models import (
    Article,
    ArticleComment,
    ArticleLike,
    ArticleTag,
    Tag,
)
from app.modules.articles.models import (
    ArticleCategory as ArticleCategoryORM,
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
from app.modules.auth.schemas import ProfileInfo
from app.modules.auth.snapshot import (
    get_user_snapshot_batch,
    profile_info_from_snap,
)
from app.modules.points.rules import enqueue_points_event

# 默认阅读速度：中文约 300 字/分钟
READING_SPEED_CPS = 300


def estimate_reading_time(content: str) -> int:
    """按中文字符数估算阅读分钟数（不足 1 分钟计 1；空内容为 0）。"""
    text_length = len(content)
    if not text_length:
        return 0
    return max(1, round(text_length / READING_SPEED_CPS))


async def _invalidate_article_cache(db: AsyncSession, slug: str) -> None:
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
    db: AsyncSession, article_id: uuid.UUID, names: list[str]
) -> None:
    """按 name upsert Tag 并关联 ArticleTag（幂等，批量 O(log N)，保序去重）。

    相比逐 tag 查/插的旧实现：tag 存在性 1 次批量查 + 缺失 tag 一次批量插（on
    conflict do nothing）+ 一次批量回查，关联查/插各一次，全程固定次数往返且
    保持输入 name 顺序（避免 set 迭代造成的顺序随机，修复预存的标签顺序 flaky）。
    """
    # 去空 + 保首现顺序去重（勿用 set：顺序非确定会打乱 tags 返回序）
    ordered = tag_names_sequence(names)
    if not ordered:
        return

    # 1) 批量查已存在 tag（name -> id）
    rows = (
        await db.execute(select(Tag.id, Tag.name).where(Tag.name.in_(ordered)))
    ).all()
    name_to_id = {name: tag_id for tag_id, name in rows}

    # 2) 缺失的 tag 一批插；再批量回查拿全量 id（用 on_conflict 免唯一冲突）
    missing = [n for n in ordered if n not in name_to_id]
    if missing:
        await db.execute(
            pg.insert(Tag)
            .values([{"name": n} for n in missing])
            .on_conflict_do_nothing(index_elements=["name"])
        )
        await db.flush()
        rows = (
            await db.execute(select(Tag.id, Tag.name).where(Tag.name.in_(ordered)))
        ).all()
        name_to_id = {name: tag_id for tag_id, name in rows}

    # 3) 批量查该文章的既有关联，只补缺失
    tag_ids = [name_to_id[n] for n in ordered]
    existing = (
        (
            await db.execute(
                select(ArticleTag.tag_id).where(
                    ArticleTag.article_id == article_id,
                    ArticleTag.tag_id.in_(tag_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_set = set(existing)
    for n in ordered:
        tag_id = name_to_id[n]
        if tag_id not in existing_set:
            db.add(ArticleTag(article_id=article_id, tag_id=tag_id))


async def create_article(
    db: AsyncSession,
    slug: str,
    title: str,
    category: str,
    content: str,
    published: datetime | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Article:
    # 幂等：同 slug 已存在则更新（重发 = 更新）
    existing = (
        (await db.execute(select(Article).where(Article.slug == slug)))
        .scalars()
        .first()
    )
    if existing:
        existing.title = title
        existing.category_id = await _resolve_category_id(db, category)
        existing.content = content
        if description is not None:
            existing.description = description
        await _sync_article_tags(db, existing.id, tags or [])
        existing.updated_at = now_iso()
        await db.flush()
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
    db.add(article)
    await db.flush()
    if tags:
        await _sync_article_tags(db, article.id, tags)
    await _invalidate_article_cache(db, slug)
    return article


async def list_articles(
    db: AsyncSession, page: int = 1, limit: int = 50
) -> PageData[ArticleListItem]:
    ver = await collection_version("articles")

    async def _load() -> dict[str, Any]:
        total = (
            await db.execute(select(func.count()).select_from(Article))
        ).scalar_one()
        stmt = (
            select(Article)
            .order_by(Article.published.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        items = (await db.execute(stmt)).scalars().all()
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


async def get_article(db: AsyncSession, slug: str) -> ArticleDetail:
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
        detail.reading_time = estimate_reading_time(article.content)
        return detail.model_dump()

    payload = await cached_read(make_key("articles:by_slug", slug), TTL_ITEM_S, _load)
    return ArticleDetail.model_validate(payload)


async def list_categories(db: AsyncSession) -> list[ArticleCategory]:
    """分类列表：读 article_categories 表 + 各分类文章数，缓存。返回 schema（slug/name/count）。"""

    async def _load() -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                select(ArticleCategoryORM, func.count(Article.id))
                .outerjoin(Article, Article.category_id == ArticleCategoryORM.id)
                .group_by(ArticleCategoryORM.id)
                .order_by(ArticleCategoryORM.sort.asc(), ArticleCategoryORM.id.asc())
            )
        ).all()
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


def _fts_search_stmt(q: str) -> tuple[Any, Any]:
    """返回 sqlalchemy 查询表达式，供 search_articles 使用。

    PostgreSQL 真 FTS ``to_tsvector('simple') @@ plainto_tsquery('simple')``，再 OR 一
    遍 ``ILIKE`` 子串通配：中文等连续无空格文本在 ``simple`` 分词下整段视为一个 lexeme、
    无法匹配“词内子串（如查'机器'命中'机器学习'）”，故 ILIKE 兜底保证“标题/正文子串能
    被搜到”这一既有语义一致（FTS 命中仍参与排序）。
    """
    # PG FTS 的子串兜底：共用同一 ilike 通配
    pattern = f"%{q}%"
    contains = or_(
        Article.title.ilike(pattern),
        Article.description.ilike(pattern),
        Article.content.ilike(pattern),
    )
    # PostgreSQL 真 FTS：simple 分词（中文分词效果已知受限，属 spec 取舍）。
    # 说明：vector 是 to_tsvector(...) 函数调用（非 data 列），SQLAlchemy 直接在其上
    # 拼列式 `.match()` 会把右操作数再次包一层 plainto_tsquery(...) → plainto_tsquery(
    # plainto_tsquery(...)) 双嵌套非法函数，真 PG 下 UndefinedFunction。此处用
    # `bool_op("@@")` 显式比较 tsvector @@ tsquery，两侧均已带 regconfig，幂等正确。
    vector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", Article.title, Article.description, Article.content),
    )
    query = func.plainto_tsquery("simple", q)
    fts = vector.bool_op("@@")(query)
    # FTS 命中且带相关度；仅子串命中者（无 FTS 相关度）也须给出、排序在后。
    return or_(fts, contains), func.ts_rank(vector, query)


async def search_articles(
    db: AsyncSession, q: str, page: int = 1, limit: int = 50
) -> PageData[ArticleListItem]:
    cond, rank = _fts_search_stmt(q)
    count_stmt = select(func.count()).select_from(Article).where(cond)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = select(Article).where(cond)
    # 仅子串命中（无 FTS 相关度）为 NULL → 排到 FTS 命中之后
    stmt = stmt.order_by(rank.desc().nulls_last()).offset((page - 1) * limit).limit(limit)
    items = (await db.execute(stmt)).scalars().all()
    return PageData(
        items=[ArticleListItem.model_validate(a) for a in items],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def list_tags(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Tag.name, func.count(ArticleTag.article_id))
            .join(ArticleTag, ArticleTag.tag_id == Tag.id)
            .group_by(Tag.id)
        )
    ).all()
    return [{"name": name, "article_count": count} for name, count in rows]


async def get_about() -> dict[str, str]:
    return {
        "title": "LKM 官方博客",
        "description": "LKM 团队博客，发布技术文章与官方资讯。",
        "maintainer": "LKM",
    }


async def _bump_article_count(
    db: AsyncSession, article_id: uuid.UUID, column: str, delta: int
) -> None:
    """原子回填计数列（SET col = col ± N），防并发丢更新。"""
    await db.execute(
        update(Article)
        .where(Article.id == article_id)
        .values({column: getattr(Article, column) + delta})
    )


async def toggle_article_like(
    db: AsyncSession, slug: str, user_id: uuid.UUID
) -> dict[str, Any]:
    article = await get_or_raise(
        db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
    )
    existing = (
        (
            await db.execute(
                select(ArticleLike).where(
                    ArticleLike.article_id == article.id,
                    ArticleLike.user_id == user_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        await db.delete(existing)
        await db.flush()
        await _bump_article_count(db, article.id, "likes", -1)
        liked = False
    else:
        db.add(ArticleLike(article_id=article.id, user_id=user_id))
        await db.flush()
        await _bump_article_count(db, article.id, "likes", 1)
        liked = True
        # 仅新增点赞路径入队（取消点赞不重复计分）
        await enqueue_points_event(db, user_id, "like", f"article:{article.id}")
    like_count = (
        await db.execute(
            select(func.count())
            .select_from(ArticleLike)
            .where(ArticleLike.article_id == article.id)
        )
    ).scalar_one()
    return {"liked": liked, "like_count": like_count}


async def create_article_comment(
    db: AsyncSession,
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
    db.add(comment)
    await db.flush()
    await _bump_article_count(db, article.id, "comments", 1)
    return comment


async def _get_author_profiles(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProfileInfo | None]:
    """批量取评论作者 ProfileInfo（M3.A残项：经 auth 批量读缝一次查齐，不再直读 Profile）。"""
    if not user_ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(user_ids))
    return {uid: profile_info_from_snap(snaps[uid]) for uid in snaps}


async def list_article_comments(db: AsyncSession, slug: str) -> list[ArticleCommentOut]:
    article = await get_or_raise(
        db, Article, ArticleErr.NOT_FOUND, Article.slug == slug
    )
    rows = (
        (
            await db.execute(
                select(ArticleComment)
                .where(ArticleComment.article_id == article.id)
                .order_by(ArticleComment.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    user_ids = {c.user_id for c in rows}
    profiles = await _get_author_profiles(db, user_ids)
    return [
        ArticleCommentOut.model_validate(c).model_copy(
            update={"profile": profiles.get(c.user_id)}
        )
        for c in rows
    ]


async def delete_article_comment(
    db: AsyncSession,
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
    await db.delete(comment)
    await _bump_article_count(db, comment.article_id, "comments", -1)
    return author_id


# ————— 分类 CRUD（写操作走 service，读列表复用 list_categories） —————


async def _invalidate_categories_cache() -> None:
    """分类变更后使分类列表缓存失效（单键删除，集合版本由 _invalidate_article_cache 负责）。"""
    await cache_invalidate(make_key("articles:categories", "ver"))


async def create_category_ex(db: AsyncSession, info: CategoryCreate) -> CategoryOut:
    """新建分类；slug 冲突抛出 409。"""
    conflict = await db.scalar(
        select(ArticleCategoryORM.id).where(ArticleCategoryORM.slug == info.slug)
    )
    if conflict is not None:
        raise BizError(ArticleErr.SLUG_CONFLICT)
    cat = ArticleCategoryORM(slug=info.slug, title=info.title, sort=info.sort)
    db.add(cat)
    await db.flush()
    await _invalidate_categories_cache()
    return CategoryOut.model_validate(cat)


async def update_category_ex(
    db: AsyncSession, category_id: uuid.UUID, patch: CategoryCreate
) -> CategoryOut:
    """更新分类；slug 冲突（排除自身）抛出 409。"""
    cat = await get_or_raise(
        db,
        ArticleCategoryORM,
        ArticleErr.CATEGORY_NOT_FOUND,
        ArticleCategoryORM.id == category_id,
    )
    conflict = await db.scalar(
        select(ArticleCategoryORM.id).where(
            ArticleCategoryORM.slug == patch.slug,
            ArticleCategoryORM.id != category_id,
        )
    )
    if conflict is not None:
        raise BizError(ArticleErr.SLUG_CONFLICT)
    cat.slug = patch.slug
    cat.title = patch.title
    cat.sort = patch.sort
    await db.flush()
    await _invalidate_categories_cache()
    return CategoryOut.model_validate(cat)


async def delete_category_ex(db: AsyncSession, category_id: uuid.UUID) -> None:
    """删除分类；分类下仍有文章时禁止删除。"""
    cat = await get_or_raise(
        db,
        ArticleCategoryORM,
        ArticleErr.CATEGORY_NOT_FOUND,
        ArticleCategoryORM.id == category_id,
    )
    used = await db.scalar(
        select(func.count(Article.id)).where(Article.category_id == category_id)
    )
    if used:
        raise BizError(CommonErr.INVALID_INPUT, "分类下仍有文章，不可删除")
    await db.delete(cat)
    await db.flush()
    await _invalidate_categories_cache()


async def _resolve_category_id(db: AsyncSession, slug: str) -> uuid.UUID:
    """按 slug 解析分类 id（旧 blog/seed 流程传 slug，这里保向兼容）；不存在则 404。"""
    category_id = await db.scalar(
        select(ArticleCategoryORM.id).where(ArticleCategoryORM.slug == slug)
    )
    if category_id is None:
        raise BizError(ArticleErr.CATEGORY_NOT_FOUND)
    return category_id


# ————— 文章写接口 / 删除 / 审核 —————


async def _require_category(db: AsyncSession, category_id: uuid.UUID) -> None:
    """校验分类存在，否则抛出 404。"""
    exists = await db.scalar(
        select(ArticleCategoryORM.id).where(ArticleCategoryORM.id == category_id)
    )
    if exists is None:
        raise BizError(ArticleErr.CATEGORY_NOT_FOUND)


async def _get_article(db: AsyncSession, slug: str) -> Article:
    """按 slug 取文章，不存在则抛出 404。"""
    return await get_or_raise(db, Article, ArticleErr.NOT_FOUND, Article.slug == slug)


async def _load_category_title(db: AsyncSession, category_id: uuid.UUID) -> str:
    """一次查询分类 title，供详情填充 category_title。"""
    title = await db.scalar(
        select(ArticleCategoryORM.title).where(ArticleCategoryORM.id == category_id)
    )
    return str(title) if title is not None else ""


async def _article_to_detail(db: AsyncSession, article: Article) -> ArticleDetail:
    """把 Article ORM 组装为 ArticleDetail，填充 category_title 与阅读时长。"""
    # article.tags 是 lazy="selectin" 的异步关系：调用方常以刚 flush/新创建
    # 的 Article 传入（tags 未预载）。若在此同步访问 article.tags 会在 async
    # 会话中触发懒加载而抛 MissingGreenlet，故先显式 refresh 按需加载该关系。
    await db.refresh(article, attribute_names=["tags"])
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


async def create_article_ex(db: AsyncSession, info: ArticleCreate) -> ArticleDetail:
    """创建文章：slug 冲突与分类存在性校验；status=published 即填充发布时间。"""
    conflict = await db.scalar(select(Article.id).where(Article.slug == info.slug))
    if conflict is not None:
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
    db.add(article)
    await db.flush()
    await _sync_article_tags(db, article.id, info.tags)
    await _invalidate_article_cache(db, info.slug)
    return await _article_to_detail(db, article)


async def update_article_ex(
    db: AsyncSession, slug: str, patch: ArticleUpdate, is_super: bool
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
    for k, v in data.items():
        setattr(article, k, v)
    if patch.tags is not None:
        await _sync_article_tags(db, article.id, patch.tags)
    await db.flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)


async def soft_delete_article(db: AsyncSession, slug: str) -> ArticleDetail:
    """软删：status 置为 rejected，清空 published。"""
    article = await _get_article(db, slug)
    article.status = "rejected"
    article.published = None
    await db.flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)


async def hard_delete_article(db: AsyncSession, slug: str) -> None:
    """硬删：published/pending 状态的文章禁止硬删（避免已展示/待审内容被直接破坏）。"""
    article = await _get_article(db, slug)
    if article.status in ("published", "pending"):
        raise BizError(ArticleErr.CANNOT_HARD_DELETE_PUBLISHED)
    # 级联删关联。article_tag/article_comments/article_likes 对 article 的外键在模型层
    # 声明 ondelete="CASCADE"，交给数据库在删 article 时级联清子行。ORM 的
    # cascade/delete-orphan 只对挂进 relationship 集合的对象生效，而本服务以
    # ``db.add(<独立关联对象>)`` 落盘子行，追不到——若不加 DB 级 CASCADE，PG 下
    # DELETE articles 会被 NO ACTION 外键拦截、遗下孤儿。
    await db.delete(article)
    await db.flush()
    await _invalidate_article_cache(db, slug)


async def review_article(db: AsyncSession, slug: str, approve: bool) -> ArticleDetail:
    """审核：仅 pending 可审；approve→published（填发布时间），否则 rejected。"""
    article = await _get_article(db, slug)
    if article.status != "pending":
        raise BizError(ArticleErr.INVALID_STATUS_TRANSITION)
    article.status = "published" if approve else "rejected"
    if approve:
        article.published = now_iso()
    await db.flush()
    await _invalidate_article_cache(db, slug)
    return await _article_to_detail(db, article)
