"""问答（QA）域仓储：提问/回答/配图的查询与写入。

步骤 9 切片 1 自 ``content.repository`` 迁出，SQL 与锁语义逐字保留。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import case, func, select

from app.modules.qa.models import QAAnswer, QAQuestion, QAQuestionImage
from core.db.repository import AsyncRepository


class QAQuestionRepository(AsyncRepository[QAQuestion]):
    model = QAQuestion

    async def list_with_answer_counts(
        self,
        *,
        category: str | None = None,
        author_id: uuid.UUID | None = None,
        sort: str = "newest",
        offset: int = 0,
        limit: int = 20,
    ) -> list[tuple[QAQuestion, int]]:
        """问题 + 回答数（外连接聚合），支持按最新或悬赏金额排序。"""
        stmt = select(QAQuestion, func.count(QAAnswer.id)).outerjoin(
            QAAnswer, QAAnswer.question_id == QAQuestion.id
        )
        if category:
            stmt = stmt.where(QAQuestion.category == category)
        if author_id:
            stmt = stmt.where(QAQuestion.author_id == author_id)
        if sort == "bounty":
            order = (QAQuestion.bounty_total.desc(), QAQuestion.id.desc())
        else:
            active_urgent = case(
                (
                    (QAQuestion.status == "open")
                    & QAQuestion.urgent
                    & (QAQuestion.bounty_expires_at > func.now()),
                    True,
                ),
                else_=False,
            )
            order = (active_urgent.desc(), QAQuestion.id.desc())
        stmt = stmt.group_by(QAQuestion.id).order_by(*order).offset(offset).limit(limit)
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    async def get_locked(self, question_id: uuid.UUID) -> QAQuestion | None:
        return (
            await self.db.execute(
                select(QAQuestion).where(QAQuestion.id == question_id).with_for_update()
            )
        ).scalar_one_or_none()

    async def expired_for_update(
        self, now: datetime.datetime, limit: int = 100
    ) -> list[QAQuestion]:
        result = await self.db.execute(
            select(QAQuestion)
            .where(
                QAQuestion.status == "open",
                QAQuestion.bounty_expires_at.is_not(None),
                QAQuestion.bounty_expires_at <= now,
            )
            .order_by(QAQuestion.bounty_expires_at, QAQuestion.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

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

    async def image_by_id(self, image_id: uuid.UUID) -> QAQuestionImage | None:
        return await self.db.scalar(
            select(QAQuestionImage).where(QAQuestionImage.id == image_id)
        )

    async def create_image(
        self, question_id: uuid.UUID, image_id: uuid.UUID, url: str, sort: int
    ) -> None:
        self.db.add(
            QAQuestionImage(id=image_id, question_id=question_id, url=url, sort=sort)
        )
        await self.db.flush()


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
