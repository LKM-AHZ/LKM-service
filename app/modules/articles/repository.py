"""articles 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**articles 域的领域查询**（多表 join、聚合、FTS/ILIKE 组合检索、原子计数回填、
标签 upsert 关联），非 articles 用的查询不往这里加。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select

from app.core.common import tag_names_sequence
from app.db.repository import AsyncRepository
from app.modules.articles.models import (
    Article,
    ArticleCategory,
    ArticleComment,
    ArticleLike,
    ArticleTag,
    Tag,
)


def _fts_search_stmt(q: str) -> tuple[Any, Any]:
    """返回 sqlalchemy 查询表达式，供 :meth:`ArticleRepository.list_for_search` 使用。

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


class ArticleRepository(AsyncRepository[Article]):
    model = Article

    async def get_by_slug(self, slug: str) -> Article | None:
        return await self.get_one(Article.slug == slug)

    async def slug_taken(self, slug: str) -> bool:
        return await self.exists(Article.slug == slug)

    async def list_page(self, *, offset: int, limit: int) -> list[Article]:
        return await self.get_many(
            order_by=Article.published.desc(), offset=offset, limit=limit
        )

    async def list_for_search(
        self, q: str, *, offset: int, limit: int
    ) -> tuple[list[Article], int]:
        """FTS + ILIKE 检索一页，返回 ``(items, total)``（items 按相关度倒序）。"""
        cond, rank = _fts_search_stmt(q)
        total = (
            await self.db.scalar(select(func.count()).select_from(Article).where(cond))
            or 0
        )
        stmt = (
            select(Article)
            .where(cond)
            # 仅子串命中（无 FTS 相关度）为 NULL → 排到 FTS 命中之后
            .order_by(rank.desc().nulls_last())
            .offset(offset)
            .limit(limit)
        )
        items = list((await self.db.execute(stmt)).scalars().all())
        return items, total

    async def bump_count(self, article_id: uuid.UUID, column: str, delta: int) -> None:
        """原子回填计数列（SET col = col ± N），防并发丢更新。"""
        await self.update_where(
            {column: getattr(Article, column) + delta}, Article.id == article_id
        )

    async def count_in_category(self, category_id: uuid.UUID) -> int:
        return await self.count(Article.category_id == category_id)

    async def refresh_tags(self, article: Article) -> None:
        """显式预载 ``tags`` 关系（异步会话下防序列化时懒加载 MissingGreenlet）。"""
        await self.db.refresh(article, attribute_names=["tags"])


class ArticleCommentRepository(AsyncRepository[ArticleComment]):
    model = ArticleComment

    async def list_in_article(self, article_id: uuid.UUID) -> list[ArticleComment]:
        return await self.get_many(
            ArticleComment.article_id == article_id,
            order_by=ArticleComment.created_at.asc(),
        )

    async def soft_delete_subtree(self, root_id: uuid.UUID) -> int:
        """软删评论及其**全部后代**，返回受影响行数。

        与硬删时代的 ORM ``cascade="all, delete-orphan"`` 等价（删根即整棵回复树从
        列表消失）；打 ``deleted_at`` 不删行，故须自行用 recursive CTE 收集后代。
        """
        tree = (
            select(ArticleComment.id)
            .where(ArticleComment.id == root_id)
            .cte("article_comment_tree", recursive=True)
        )
        tree = tree.union_all(
            select(ArticleComment.id).where(ArticleComment.parent_id == tree.c.id)
        )
        return await self.soft_delete_where(ArticleComment.id.in_(select(tree.c.id)))


class ArticleLikeRepository(AsyncRepository[ArticleLike]):
    model = ArticleLike

    async def get_one_like(
        self, *, article_id: uuid.UUID, user_id: uuid.UUID
    ) -> ArticleLike | None:
        return await self.get_one(
            ArticleLike.article_id == article_id, ArticleLike.user_id == user_id
        )

    async def count_for(self, article_id: uuid.UUID) -> int:
        return await self.count(ArticleLike.article_id == article_id)


class ArticleCategoryRepository(AsyncRepository[ArticleCategory]):
    model = ArticleCategory

    async def slug_taken(
        self, slug: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        """slug 是否已被占用（可排除自身，供更新时判重）。"""
        conditions = [ArticleCategory.slug == slug]
        if exclude_id is not None:
            conditions.append(ArticleCategory.id != exclude_id)
        return await self.exists(*conditions)

    async def id_by_slug(self, slug: str) -> uuid.UUID | None:
        return await self.db.scalar(
            select(ArticleCategory.id).where(ArticleCategory.slug == slug)
        )

    async def title_by_id(self, category_id: uuid.UUID) -> str | None:
        return await self.db.scalar(
            select(ArticleCategory.title).where(ArticleCategory.id == category_id)
        )

    async def id_exists(self, category_id: uuid.UUID) -> bool:
        return await self.exists(ArticleCategory.id == category_id)

    async def list_with_article_counts(
        self,
    ) -> list[tuple[ArticleCategory, int]]:
        """分类 + 各分类文章数（外连接聚合），按 sort/id 升序。"""
        rows = (
            await self.db.execute(
                select(ArticleCategory, func.count(Article.id))
                .outerjoin(Article, Article.category_id == ArticleCategory.id)
                .group_by(ArticleCategory.id)
                .order_by(ArticleCategory.sort.asc(), ArticleCategory.id.asc())
            )
        ).all()
        return [(cat, count) for cat, count in rows]


class TagRepository(AsyncRepository[Tag]):
    model = Tag

    async def name_to_id(self, names: list[str]) -> dict[str, uuid.UUID]:
        rows = (
            await self.db.execute(select(Tag.id, Tag.name).where(Tag.name.in_(names)))
        ).all()
        return {name: tag_id for tag_id, name in rows}


class ArticleTagRepository(AsyncRepository[ArticleTag]):
    model = ArticleTag

    async def sync_for_article(self, article_id: uuid.UUID, names: list[str]) -> None:
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
        tag_repo = TagRepository(self.db)
        name_to_id = await tag_repo.name_to_id(ordered)

        # 2) 缺失的 tag 一批插；再批量回查拿全量 id（用 on_conflict 免唯一冲突）
        missing = [n for n in ordered if n not in name_to_id]
        if missing:
            await tag_repo.pg_upsert(
                [{"name": n} for n in missing],
                index_elements=["name"],
                do_nothing=True,
            )
            name_to_id = await tag_repo.name_to_id(ordered)

        # 3) 批量查该文章的既有关联，只补缺失
        tag_ids = [name_to_id[n] for n in ordered]
        existing = set(
            (
                await self.db.execute(
                    select(ArticleTag.tag_id).where(
                        ArticleTag.article_id == article_id,
                        ArticleTag.tag_id.in_(tag_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for n in ordered:
            tag_id = name_to_id[n]
            if tag_id not in existing:
                self.db.add(ArticleTag(article_id=article_id, tag_id=tag_id))

    async def list_tag_counts(self) -> list[tuple[str, int]]:
        """标签名 + 被引用文章数（内连接聚合）。"""
        rows = (
            await self.db.execute(
                select(Tag.name, func.count(ArticleTag.article_id))
                .join(ArticleTag, ArticleTag.tag_id == Tag.id)
                .group_by(Tag.id)
            )
        ).all()
        return [(name, count) for name, count in rows]
