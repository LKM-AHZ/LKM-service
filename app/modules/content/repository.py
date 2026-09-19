"""content 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**content 域的领域查询**（多表 join、聚合、排序窗口、批量 in、原子自增），不做
过度抽象——非 content 用的查询不往这里加。

内容/评论域的软删过滤由基类按「模型是否真有 ``deleted_at`` 列」动态施加（批 4 之前
是零副作用）；需要墓碑语义（如 slug 占位防复活）的查询显式 ``include_deleted=True``。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Select, func, select

from app.db.repository import AsyncRepository
from app.modules.content.models import (
    Board,
    BoardApplication,
    BoardBan,
    Column,
    ColumnApplication,
    ColumnPost,
    ContentComment,
    ContentItem,
    ContentLike,
    ContentStatus,
    ContentType,
    QAAnswer,
    QAQuestion,
    QAQuestionImage,
)
from app.modules.exam.models import Exam, ExamCertificate


class ContentItemRepository(AsyncRepository[ContentItem]):
    model = ContentItem

    @staticmethod
    def _published_conditions(
        board_id: uuid.UUID | None, content_type: str | None
    ) -> list[object]:
        conditions: list[object] = [ContentItem.status == ContentStatus.PUBLISHED]
        if board_id:
            conditions.append(ContentItem.board_id == board_id)
        if content_type:
            conditions.append(ContentItem.content_type == content_type)
        return conditions

    async def count_published(
        self,
        *,
        board_id: uuid.UUID | None = None,
        content_type: str | None = None,
    ) -> int:
        return await self.count(*self._published_conditions(board_id, content_type))

    async def list_published(
        self,
        *,
        board_id: uuid.UUID | None = None,
        content_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ContentItem]:
        return await self.get_many(
            *self._published_conditions(board_id, content_type),
            order_by=(ContentItem.is_pinned.desc(), ContentItem.id.desc()),
            offset=offset,
            limit=limit,
        )

    async def get_by_slug(self, slug: str) -> ContentItem | None:
        """按 slug 取活跃条目（软删条目不返回）。"""
        return await self.get_one(ContentItem.slug == slug)

    async def slug_taken(self, slug: str) -> bool:
        """slug 是否已被占用——**含**已软删行：墓碑占位，防删除后同 slug 复活歧义。"""
        return await self.exists(ContentItem.slug == slug, include_deleted=True)

    async def bump_view_count(self, item_id: uuid.UUID) -> None:
        """原子 ``view_count + 1``（避免并发 read-modify-write 丢计数）。"""
        await self.update_where(
            {"view_count": ContentItem.view_count + 1}, ContentItem.id == item_id
        )

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


class ContentCommentRepository(AsyncRepository[ContentComment]):
    model = ContentComment

    async def count_in_content(self, item_id: uuid.UUID) -> int:
        return await self.count(ContentComment.content_id == item_id)

    async def list_in_content(
        self, item_id: uuid.UUID, *, offset: int = 0, limit: int = 20
    ) -> list[ContentComment]:
        return await self.get_many(
            ContentComment.content_id == item_id,
            order_by=ContentComment.floor_number.asc(),
            offset=offset,
            limit=limit,
        )

    async def list_all_in_content(self, item_id: uuid.UUID) -> list[ContentComment]:
        return await self.get_many(
            ContentComment.content_id == item_id,
            order_by=ContentComment.floor_number.asc(),
        )

    async def max_floor_number(self, item_id: uuid.UUID) -> int | None:
        """当前最大楼层号；**含已软删**——否则软删后新评论会与可见楼层重号。"""
        return await self.db.scalar(
            select(func.max(ContentComment.floor_number)).where(
                ContentComment.content_id == item_id
            )
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
        self, *, offset: int | None = None, limit: int | None = None
    ) -> list[Column]:
        return await self.get_many(
            order_by=Column.id.desc(), offset=offset, limit=limit
        )


class ColumnApplicationRepository(AsyncRepository[ColumnApplication]):
    model = ColumnApplication

    async def list_page(
        self, *, offset: int | None = None, limit: int | None = None
    ) -> list[ColumnApplication]:
        return await self.get_many(
            order_by=ColumnApplication.id.desc(), offset=offset, limit=limit
        )


class ColumnPostRepository(AsyncRepository[ColumnPost]):
    model = ColumnPost

    async def list_in_column(
        self,
        column_id: uuid.UUID,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> list[ColumnPost]:
        return await self.get_many(
            ColumnPost.column_id == column_id,
            order_by=ColumnPost.id.desc(),
            offset=offset,
            limit=limit,
        )


class BoardRepository(AsyncRepository[Board]):
    model = Board

    async def get_by_slug(self, slug: str) -> Board | None:
        return await self.get_one(Board.slug == slug)

    async def slug_taken(self, slug: str) -> bool:
        return await self.exists(Board.slug == slug)

    async def list_ordered(self) -> list[Board]:
        return await self.get_many(order_by=Board.id.asc())

    async def has_normal_certification(self, user_id: uuid.UUID) -> bool:
        """该用户是否持有初级通识认证证书（type=exam 且 unlock_level=normal）。

        证书表属 exam 域，content 侧只读判定（跨模块读缝已在 import-linter 豁免）。
        """
        stmt: Select[object] = (
            select(ExamCertificate.id)
            .join(Exam, Exam.id == ExamCertificate.exam_id)
            .where(
                ExamCertificate.user_id == user_id,
                ExamCertificate.passed.is_(True),
                Exam.type == "exam",
                Exam.unlock_level == "normal",
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None


class BoardApplicationRepository(AsyncRepository[BoardApplication]):
    model = BoardApplication

    async def slug_taken(self, slug: str) -> bool:
        return await self.exists(BoardApplication.slug == slug)


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


class QAQuestionRepository(AsyncRepository[QAQuestion]):
    model = QAQuestion

    async def list_with_answer_counts(
        self, *, category: str | None = None, offset: int = 0, limit: int = 20
    ) -> list[tuple[QAQuestion, int]]:
        """问题 + 回答数（外连接聚合），按 id 倒序分页。"""
        stmt = select(QAQuestion, func.count(QAAnswer.id)).outerjoin(
            QAAnswer, QAAnswer.question_id == QAQuestion.id
        )
        if category:
            stmt = stmt.where(QAQuestion.category == category)
        stmt = (
            stmt.group_by(QAQuestion.id)
            .order_by(QAQuestion.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    async def add_images(self, question_id: uuid.UUID, urls: list[str]) -> None:
        """批量落提问配图（原顺序即 ``sort``），一次 flush。"""
        for index, url in enumerate(urls):
            self.db.add(QAQuestionImage(question_id=question_id, url=url, sort=index))
        await self.db.flush()

    async def list_images(self, question_id: uuid.UUID) -> list[QAQuestionImage]:
        return list(
            (
                await self.db.execute(
                    select(QAQuestionImage)
                    .where(QAQuestionImage.question_id == question_id)
                    .order_by(QAQuestionImage.sort.asc())
                )
            )
            .scalars()
            .all()
        )


class QAAnswerRepository(AsyncRepository[QAAnswer]):
    model = QAAnswer

    async def list_in_question(self, question_id: uuid.UUID) -> list[QAAnswer]:
        return await self.get_many(
            QAAnswer.question_id == question_id, order_by=QAAnswer.id.asc()
        )

    async def count_accepted(self, question_id: uuid.UUID) -> int:
        return await self.count(
            QAAnswer.question_id == question_id, QAAnswer.is_accepted.is_(True)
        )
