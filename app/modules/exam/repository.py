"""exam 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**exam 域的领域查询**（题目预载、成绩单 join、证书判定、榜单排序），非 exam 用的
查询不往这里加。``points.rules`` / ``auth.snapshot`` / ``auth.service_authz`` 等
跨模块**服务调用**仍留在 service 编排层，不在此收编。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.err import BizError, ErrCode
from app.db.repository import AsyncRepository
from app.modules.exam.models import Exam, ExamAttempt, ExamCertificate, ExamQuestion


def _question_options() -> tuple[Any, ...]:
    """题目预载，避免 async 会话里 lazy 访问 ``exam.questions``。"""
    return (selectinload(Exam.questions),)


class ExamRepository(AsyncRepository[Exam]):
    model = Exam

    async def get_with_questions(self, exam_id: uuid.UUID) -> Exam | None:
        return await self.get_one(Exam.id == exam_id, options=_question_options())

    async def get_with_questions_or_raise(
        self, exam_id: uuid.UUID, errcode: ErrCode, *, detail: str | None = None
    ) -> Exam:
        exam = await self.get_with_questions(exam_id)
        if exam is None:
            raise BizError(errcode, detail)
        return exam

    async def list_page(
        self, *, type_: str | None, offset: int, limit: int
    ) -> tuple[list[Exam], int]:
        """公开考试分页（题目预载），返回 ``(items, total)``。"""
        conditions: list[Any] = []
        if type_:
            conditions.append(Exam.type == type_)
        total = await self.count(*conditions)
        items = await self.get_many(
            *conditions,
            order_by=Exam.id.desc(),
            offset=offset,
            limit=limit,
            options=_question_options(),
        )
        return items, total


class ExamAttemptRepository(AsyncRepository[ExamAttempt]):
    model = ExamAttempt


class ExamQuestionRepository(AsyncRepository[ExamQuestion]):
    model = ExamQuestion

    async def add_all(self, questions: list[ExamQuestion]) -> None:
        """批量落题并一次 flush（建考时按序写 ``sort_order``）。"""
        for question in questions:
            self.db.add(question)
        await self.flush()


class ExamCertificateRepository(AsyncRepository[ExamCertificate]):
    model = ExamCertificate

    async def passed_exists(self, *, exam_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """该用户是否已通过这场考试（防重复解锁/重复交卷）。"""
        return await self.exists(
            ExamCertificate.exam_id == exam_id,
            ExamCertificate.user_id == user_id,
            ExamCertificate.passed.is_(True),
        )

    async def list_for_user(
        self, user_id: uuid.UUID
    ) -> list[tuple[ExamCertificate, str | None]]:
        """某用户全部证书（join 考试标题），按发证时间倒序。"""
        rows = (
            await self.db.execute(
                select(ExamCertificate, Exam.title)
                .join(Exam, Exam.id == ExamCertificate.exam_id)
                .where(ExamCertificate.user_id == user_id)
                .order_by(ExamCertificate.issued_at.desc())
            )
        ).all()
        return [(cert, title) for cert, title in rows]

    async def list_for_exam(self, exam_id: uuid.UUID) -> list[ExamCertificate]:
        """某场考试的全部证书，按成绩降序、同分早发证者在前。"""
        return await self.get_many(
            ExamCertificate.exam_id == exam_id,
            order_by=(ExamCertificate.score.desc(), ExamCertificate.issued_at.asc()),
        )
