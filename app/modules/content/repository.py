"""content 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`core.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**content 域的领域查询**（多表 join、聚合、排序窗口、批量 in、原子自增），不做
过度抽象——非 content 用的查询不往这里加。

内容/评论域的软删过滤由基类按「模型是否真有 ``deleted_at`` 列」动态施加（批 4 之前
是零副作用）；需要墓碑语义（如 slug 占位防复活）的查询显式 ``include_deleted=True``。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.modules.content.errors import ContentErr
from app.modules.content.models import (
    Board,
    BoardApplication,
    BoardBan,
    Column,
    ColumnApplication,
    ColumnPost,
    ContentItem,
    ContentLike,
    ContentStatus,
    ContentType,
)
from core.db.repository import AsyncRepository
from core.err import BizError, CommonErr
from core.ports import exam as exam_port


class ContentItemRepository(AsyncRepository[ContentItem]):
    model = ContentItem

    @staticmethod
    def _published_conditions(
        board_id: uuid.UUID | None,
        content_type: str | None,
        author_id: uuid.UUID | None = None,
    ) -> list[object]:
        conditions: list[object] = [ContentItem.status == ContentStatus.PUBLISHED]
        if board_id:
            conditions.append(ContentItem.board_id == board_id)
        if content_type:
            conditions.append(ContentItem.content_type == content_type)
        if author_id:
            conditions.append(ContentItem.author_id == author_id)
        return conditions

    async def count_published(
        self,
        *,
        board_id: uuid.UUID | None = None,
        content_type: str | None = None,
        author_id: uuid.UUID | None = None,
    ) -> int:
        return await self.count(
            *self._published_conditions(board_id, content_type, author_id)
        )

    async def list_published(
        self,
        *,
        board_id: uuid.UUID | None = None,
        content_type: str | None = None,
        author_id: uuid.UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ContentItem]:
        return await self.get_many(
            *self._published_conditions(board_id, content_type, author_id),
            order_by=(ContentItem.is_pinned.desc(), ContentItem.id.desc()),
            offset=offset,
            limit=limit,
        )

    async def get_by_slug(self, slug: str) -> ContentItem | None:
        """按 slug 取活跃条目（软删条目不返回）。"""
        return await self.get_one(ContentItem.slug == slug)

    async def lock_active(self, item_id: uuid.UUID) -> None:
        """只读取主键并锁住未删除内容，串行化互动明细与楼层分配。"""
        locked_id = await self.db.scalar(
            select(ContentItem.id)
            .where(ContentItem.id == item_id, ContentItem.deleted_at.is_(None))
            .with_for_update()
        )
        if locked_id is None:
            raise BizError(ContentErr.CONTENT_NOT_FOUND)

    async def id_by_slug(self, slug: str) -> uuid.UUID | None:
        """按 slug 只取 id（窄列，不拉正文）：详情缓存按 id 建键，slug 读路径先解析 id 再复用缓存。"""
        return await self.db.scalar(
            select(ContentItem.id).where(
                ContentItem.slug == slug, ContentItem.deleted_at.is_(None)
            )
        )

    async def read_counter_snapshot(
        self, item_id: uuid.UUID
    ) -> tuple[int, int, int, int, int] | None:
        """窄列读 5 个互动计数（不含正文/其它大列），供详情缓存命中时叠加**实时**计数。

        只 SELECT 计数列：正文是大 TOAST 列，不取即免去行外读与 detoast，这是详情缓存
        真正省下的开销。行不存在（含已软删）返回 ``None``。
        """
        row = (
            await self.db.execute(
                select(
                    ContentItem.view_count,
                    ContentItem.like_count,
                    ContentItem.comment_count,
                    ContentItem.bookmark_count,
                    ContentItem.forward_count,
                ).where(ContentItem.id == item_id, ContentItem.deleted_at.is_(None))
            )
        ).first()
        if row is None:
            return None
        return (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]))

    async def slug_taken(self, slug: str) -> bool:
        """slug 是否已被占用——**含**已软删行：墓碑占位，防删除后同 slug 复活歧义。"""
        return await self.exists(ContentItem.slug == slug, include_deleted=True)

    async def bump_view_count(self, item_id: uuid.UUID) -> None:
        """原子 ``view_count + 1``（避免并发 read-modify-write 丢计数）。"""
        await self.update_where(
            {"view_count": ContentItem.view_count + 1}, ContentItem.id == item_id
        )

    async def bump_forward_count(self, item_id: uuid.UUID) -> int:
        """原子 ``forward_count + 1`` 并返回新值；行不存在（含已软删）抛 ``CONTENT_NOT_FOUND``。

        ``forward_count`` 没有明细表（不参与对账，见 ``content/counters.py`` 的说明），
        故不像 like/comment/bookmark 那样走 ``bump_content_counter``，直接原子自增即可。
        返回新值是为了让调用方拿服务端真值校正本地乐观值。
        """
        result = await self.db.execute(
            sa_update(ContentItem)
            .where(ContentItem.id == item_id, ContentItem.deleted_at.is_(None))
            .values(forward_count=ContentItem.forward_count + 1)
            .returning(ContentItem.forward_count)
        )
        row = result.first()
        if row is None:
            raise BizError(ContentErr.CONTENT_NOT_FOUND)
        return int(row[0])

    async def count_daily_discussions(
        self, *, author_id: uuid.UUID, board_id: uuid.UUID, since: datetime.datetime
    ) -> int:
        """某用户在某板块自 ``since`` 起的讨论帖数（发帖日限用）。"""
        return await self.count(
            ContentItem.author_id == author_id,
            ContentItem.board_id == board_id,
            ContentItem.content_type == ContentType.DISCUSSION,
            ContentItem.created_at >= since,
        )


class ContentLikeRepository(AsyncRepository[ContentLike]):
    model = ContentLike

    async def get_one_like(
        self, *, content_id: uuid.UUID, user_id: uuid.UUID
    ) -> ContentLike | None:
        return await self.get_one(
            ContentLike.content_id == content_id, ContentLike.user_id == user_id
        )


class ColumnRepository(AsyncRepository[Column]):
    model = Column

    async def get_by_slug(self, slug: str) -> Column | None:
        return await self.get_one(Column.slug == slug)

    async def get_by_application(self, application_id: uuid.UUID) -> Column | None:
        return await self.get_one(Column.application_id == application_id)

    async def list_titles(self, column_ids: set[uuid.UUID]) -> list[Column]:
        if not column_ids:
            return []
        return await self.get_many(Column.id.in_(column_ids))

    async def list_page(
        self,
        *,
        offset: int | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> list[Column]:
        """``status`` 给定时只取该状态（公开读口把可见性下推到 SQL，避免分页后再过滤）。"""
        conditions = [Column.status == status] if status is not None else []
        return await self.get_many(
            *conditions, order_by=Column.id.desc(), offset=offset, limit=limit
        )


class ColumnApplicationRepository(AsyncRepository[ColumnApplication]):
    model = ColumnApplication

    async def list_page(
        self, *, offset: int | None = None, limit: int | None = None
    ) -> list[ColumnApplication]:
        return await self.get_many(
            order_by=ColumnApplication.id.desc(), offset=offset, limit=limit
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[ColumnApplication]:
        """申请人视角：本人全部申请，最新在前（id 为 uuid7，单调递增近似时间序）。"""
        return await self.get_many(
            ColumnApplication.user_id == user_id,
            order_by=ColumnApplication.id.desc(),
        )

    async def has_pending(self, user_id: uuid.UUID) -> bool:
        return await self.exists(
            ColumnApplication.user_id == user_id,
            ColumnApplication.status == "pending",
        )

    async def latest_rejected(self, user_id: uuid.UUID) -> ColumnApplication | None:
        """该用户最近一次被拒的申请（冷却期按它的 reviewed_at 算）。"""
        return await self.get_one(
            ColumnApplication.user_id == user_id,
            ColumnApplication.status == "rejected",
            order_by=ColumnApplication.reviewed_at.desc(),
        )


class ColumnPostRepository(AsyncRepository[ColumnPost]):
    model = ColumnPost

    async def list_in_column(
        self,
        column_id: uuid.UUID,
        *,
        offset: int | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> list[ColumnPost]:
        """``status`` 给定时只取该状态（公开读口下推可见性用）。"""
        conditions: list[object] = [ColumnPost.column_id == column_id]
        if status is not None:
            conditions.append(ColumnPost.status == status)
        return await self.get_many(
            *conditions,
            order_by=ColumnPost.id.desc(),
            offset=offset,
            limit=limit,
        )


class BoardRepository(AsyncRepository[Board]):
    model = Board
    version_snapshot_fields = ("slug", "title")

    async def get_by_slug(self, slug: str) -> Board | None:
        return await self.get_one(Board.slug == slug)

    async def slug_taken(self, slug: str) -> bool:
        return await self.exists(Board.slug == slug)

    async def ensure_by_slug(self, slug: str) -> uuid.UUID:
        """按 slug 取板块 id，不存在则原子创建（博客发布等跨域路径用）。"""
        existing = await self.get_one(Board.slug == slug)
        if existing is not None:
            return existing.id
        # 并发建同一 slug 时两边都查不到、各自 insert，后提交者撞 board.slug 唯一约束
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

    async def list_ordered(self) -> list[Board]:
        return await self.get_many(order_by=Board.id.asc())

    async def has_normal_certification(self, user_id: uuid.UUID) -> bool:
        """该用户是否持有初级通识认证证书（type=exam 且 unlock_level=normal）。

        证书表属 exam 域，content 侧只读判定（跨模块读缝已在 import-linter 豁免）。
        """
        return await exam_port.has_normal_certification(self.db, user_id)


class BoardApplicationRepository(AsyncRepository[BoardApplication]):
    model = BoardApplication

    async def slug_taken(self, slug: str) -> bool:
        """slug 是否被**非被拒**申请占用：被拒申请释放标识，允许 24h 后重提。"""
        return await self.exists(
            BoardApplication.slug == slug,
            BoardApplication.status != "rejected",
        )

    async def list_for_applicant(
        self, applicant_id: uuid.UUID
    ) -> list[BoardApplication]:
        """申请人视角：本人全部板块申请，最新在前。"""
        return await self.get_many(
            BoardApplication.applicant_id == applicant_id,
            order_by=BoardApplication.id.desc(),
        )

    async def has_pending(self, applicant_id: uuid.UUID, slug: str) -> bool:
        return await self.exists(
            BoardApplication.applicant_id == applicant_id,
            BoardApplication.slug == slug,
            BoardApplication.status == "pending",
        )

    async def latest_rejected(
        self, applicant_id: uuid.UUID, slug: str
    ) -> BoardApplication | None:
        """同一申请人同一 slug 最近一次被拒（冷却期按它的 reviewed_at 算）。"""
        return await self.get_one(
            BoardApplication.applicant_id == applicant_id,
            BoardApplication.slug == slug,
            BoardApplication.status == "rejected",
            order_by=BoardApplication.reviewed_at.desc(),
        )


class BoardBanRepository(AsyncRepository[BoardBan]):
    model = BoardBan

    async def get_active(
        self, *, board_id: uuid.UUID, user_id: uuid.UUID, now: datetime.datetime
    ) -> BoardBan | None:
        return await self.get_one(
            BoardBan.board_id == board_id,
            BoardBan.user_id == user_id,
            BoardBan.expires_at > now,
        )

    async def hard_delete_for(self, *, board_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """解封：按 (板块, 用户) 抹掉禁言台账（硬删，封禁记录不保留历史）。"""
        return await self.hard_delete_where(
            BoardBan.board_id == board_id, BoardBan.user_id == user_id
        )
