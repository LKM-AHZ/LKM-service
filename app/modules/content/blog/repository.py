"""blog 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**blog 域的领域查询**（star 计数/批量查、评论回复预载、内容行读写、隔离台账、
发布用板块 get-or-create），非 blog 用的查询不往这里加。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.err import BizError, CommonErr
from app.db.repository import AsyncRepository
from app.modules.content.blog.models import (
    BlogComment,
    BlogContent,
    BlogRepoQuarantine,
    BlogSeries,
    BlogStar,
)
from app.modules.content.models import Board


class BlogSeriesRepository(AsyncRepository[BlogSeries]):
    model = BlogSeries

    async def get_by_repo_name(self, repo_name: str) -> BlogSeries | None:
        return await self.get_one(BlogSeries.repo_name == repo_name)

    async def list_page(
        self, *, offset: int | None = None, limit: int | None = None
    ) -> list[BlogSeries]:
        return await self.get_many(
            order_by=BlogSeries.id.desc(), offset=offset, limit=limit
        )


class BlogStarRepository(AsyncRepository[BlogStar]):
    model = BlogStar

    async def get_one_star(
        self, *, series_id: uuid.UUID, user_id: uuid.UUID
    ) -> BlogStar | None:
        return await self.get_one(
            BlogStar.series_id == series_id, BlogStar.user_id == user_id
        )

    async def count_for(self, series_id: uuid.UUID) -> int:
        return await self.count(BlogStar.series_id == series_id)

    async def counts_for(self, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """批量统计多个系列的 star 数量，避免逐条查询的 N+1。"""
        if not series_ids:
            return {}
        rows = (
            await self.db.execute(
                select(BlogStar.series_id, func.count(BlogStar.user_id))
                .where(BlogStar.series_id.in_(set(series_ids)))
                .group_by(BlogStar.series_id)
            )
        ).all()
        return {sid: cnt for sid, cnt in rows}

    async def starred_ids(
        self, series_ids: list[uuid.UUID], user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """批量查当前用户 star 了哪些系列，避免逐条查询的 N+1。"""
        if not series_ids:
            return set()
        rows = (
            await self.db.execute(
                select(BlogStar.series_id).where(
                    BlogStar.series_id.in_(set(series_ids)),
                    BlogStar.user_id == user_id,
                )
            )
        ).all()
        return {sid for (sid,) in rows}


class BlogCommentRepository(AsyncRepository[BlogComment]):
    model = BlogComment

    async def list_in_series(self, series_id: uuid.UUID) -> list[BlogComment]:
        """某系列的评论（含 replies 预载，防序列化时懒加载 MissingGreenlet）。

        前提：本方法一次性取回该 series 的全部评论，第 2 层及更深的 replies 依赖这些
        对象都作为 selectinload 的 parent 被补载。若将来加「分页 / 按父节点过滤」，
        深层 replies 会在序列化时触发异步懒加载（MissingGreenlet），届时须改成
        显式递归预载或按深度分批取。"""
        return await self.get_many(
            BlogComment.series_id == series_id,
            order_by=BlogComment.created_at.asc(),
            options=(selectinload(BlogComment.replies),),
        )

    async def get_with_replies(self, comment_id: uuid.UUID) -> BlogComment | None:
        """按 id 取评论并用 selectinload 预载 replies。"""
        return await self.get_one(
            BlogComment.id == comment_id,
            options=(selectinload(BlogComment.replies),),
        )

    async def soft_delete_subtree(self, root_id: uuid.UUID) -> int:
        """软删评论及其**全部后代**，返回受影响行数。

        与硬删时代的 ORM ``cascade="all, delete-orphan"`` 等价：删根评论后整棵回复树
        从列表消失（只打 ``deleted_at`` 不删行，故须自己走 recursive CTE 收集后代）。
        """
        tree = (
            select(BlogComment.id)
            .where(BlogComment.id == root_id)
            .cte("blog_comment_tree", recursive=True)
        )
        tree = tree.union_all(
            select(BlogComment.id).where(BlogComment.parent_id == tree.c.id)
        )
        return await self.soft_delete_where(BlogComment.id.in_(select(tree.c.id)))


class BlogContentRepository(AsyncRepository[BlogContent]):
    model = BlogContent

    async def get_row(self, series_id: uuid.UUID, filepath: str) -> BlogContent | None:
        return await self.get_one(
            BlogContent.series_id == series_id, BlogContent.path == filepath
        )

    async def list_paths(self, series_id: uuid.UUID) -> list[str]:
        rows = (
            await self.db.execute(
                select(BlogContent.path).where(BlogContent.series_id == series_id)
            )
        ).scalars()
        return list(rows.all())


class BlogRepoQuarantineRepository(AsyncRepository[BlogRepoQuarantine]):
    model = BlogRepoQuarantine

    async def get_by_repo_name(self, repo_name: str) -> BlogRepoQuarantine | None:
        return await self.get_one(BlogRepoQuarantine.repo_name == repo_name)


class BoardRepository(AsyncRepository[Board]):
    """blog 发布路径用的板块 get-or-create（跨模块读缝已在 import-linter 豁免）。"""

    model = Board

    async def ensure_by_slug(self, slug: str) -> uuid.UUID:
        existing = await self.get_one(Board.slug == slug)
        if existing is not None:
            return existing.id
        # 并发发布同一 slug 时两边都查不到、各自 insert，后提交者撞 board.slug 唯一约束
        # 会让整个发布请求失败。改为原子插入 + 冲突忽略后回读（同 points 的 pg_upsert 写法）。
        await self.pg_upsert(
            {
                "slug": slug,
                "title": slug,
                "description": "auto-created for blog publish",
            },
            index_elements=["slug"],
            do_nothing=True,
        )
        board = await self.get_one(Board.slug == slug)
        if board is None:  # 仅防御：DO NOTHING + 回查理论上必命中
            raise BizError(CommonErr.INTERNAL_ERROR, f"board slug={slug} not found")
        return board.id
