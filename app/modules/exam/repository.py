"""exam 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`core.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**exam 域的领域查询**（题目预载、成绩单 join、证书判定、榜单排序、行锁），非 exam 用的
查询不往这里加。``points.rules`` / ``auth.snapshot`` / ``auth.service_authz`` 等
跨模块**服务调用**仍留在 service 编排层，不在此收编。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.modules.exam.models import (
    Exam,
    ExamAttempt,
    ExamAttemptAnswer,
    ExamCertificate,
    ExamGroupAssignment,
    ExamPrerequisite,
    ExamQuestion,
    ExamQuestionGroup,
    ExamRegistration,
    ExamTrial,
    ExamWrongAnswer,
)
from core.db.base import now_iso
from core.db.repository import AsyncRepository
from core.err import BizError


def _question_options() -> tuple[Any, ...]:
    """题目预载，避免 async 会话里 lazy 访问 ``exam.questions``。"""
    return (selectinload(Exam.questions),)


class ExamRepository(AsyncRepository[Exam]):
    model = Exam

    async def get_with_questions(self, exam_id: uuid.UUID) -> Exam | None:
        return await self.get_one(Exam.id == exam_id, options=_question_options())

    async def get_with_questions_or_raise(
        self, exam_id: uuid.UUID, errcode: Any, *, detail: str | None = None
    ) -> Exam:
        exam = await self.get_with_questions(exam_id)
        if exam is None:
            raise BizError(errcode, detail)
        return exam

    async def list_page(
        self, *, type_: str | None, offset: int, limit: int
    ) -> tuple[list[Exam], int]:
        """公开考试分页，返回 ``(items, total)``。"""
        conditions: list[Any] = [
            Exam.is_published.is_(True),
            Exam.is_listed.is_(True),
            Exam.is_archived.is_(False),
        ]
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

    async def list_questions_for_exam(
        self, exam_id: uuid.UUID, *, include_unapproved: bool = False
    ) -> list[ExamQuestion]:
        """试卷题目：优先题组分配；无分配行时回退 ``exam_questions.exam_id``。

        返回顺序遵循「题组排序 → 题目 sort_order」，保证快照稳定。
        """
        assignments = await ExamGroupAssignmentRepository(self.db).list_for_exam(
            exam_id
        )
        if assignments:
            # 已软删的题组不再参与新快照；其题目行保留（错题/历史引用仍可读）。
            live = await ExamQuestionGroupRepository(self.db).get_many(
                ExamQuestionGroup.id.in_([a.group_id for a in assignments])
            )
            live_ids = {g.id for g in live}
            assignments = [a for a in assignments if a.group_id in live_ids]
        conditions: list[Any] = [ExamQuestion.deleted_at.is_(None)]
        if not include_unapproved:
            conditions.append(ExamQuestion.review_status == "approved")
        if assignments:
            group_ids = [a.group_id for a in assignments]
            rows = await ExamQuestionRepository(self.db).get_many(
                ExamQuestion.group_id.in_(group_ids), *conditions
            )
            order = {gid: idx for idx, gid in enumerate(group_ids)}
            return sorted(
                rows,
                key=lambda q: (order.get(q.group_id, len(order)), q.sort_order, q.id),
            )
        return await ExamQuestionRepository(self.db).get_many(
            ExamQuestion.exam_id == exam_id,
            *conditions,
            order_by=(ExamQuestion.sort_order, ExamQuestion.id),
        )

    async def count_questions(self, exam_id: uuid.UUID) -> int:
        assignments = await ExamGroupAssignmentRepository(self.db).list_for_exam(
            exam_id
        )
        if assignments:
            live = await ExamQuestionGroupRepository(self.db).get_many(
                ExamQuestionGroup.id.in_([a.group_id for a in assignments])
            )
            live_ids = {g.id for g in live}
            assignments = [a for a in assignments if a.group_id in live_ids]
        conditions: list[Any] = [
            ExamQuestion.deleted_at.is_(None),
            ExamQuestion.review_status == "approved",
        ]
        if assignments:
            conditions.append(
                ExamQuestion.group_id.in_([a.group_id for a in assignments])
            )
        else:
            conditions.append(ExamQuestion.exam_id == exam_id)
        return await ExamQuestionRepository(self.db).count(*conditions)


class ExamQuestionGroupRepository(AsyncRepository[ExamQuestionGroup]):
    model = ExamQuestionGroup

    async def key_taken(self, key: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        conditions: list[Any] = [ExamQuestionGroup.key == key]
        if exclude_id is not None:
            conditions.append(ExamQuestionGroup.id != exclude_id)
        return await self.exists(*conditions)

    async def question_count(self, group_id: uuid.UUID) -> int:
        return await ExamQuestionRepository(self.db).count(
            ExamQuestion.group_id == group_id, ExamQuestion.deleted_at.is_(None)
        )


class ExamGroupAssignmentRepository(AsyncRepository[ExamGroupAssignment]):
    model = ExamGroupAssignment

    async def list_for_exam(self, exam_id: uuid.UUID) -> list[ExamGroupAssignment]:
        return await self.get_many(
            ExamGroupAssignment.exam_id == exam_id,
            order_by=(ExamGroupAssignment.sort_order, ExamGroupAssignment.id),
        )


class ExamQuestionRepository(AsyncRepository[ExamQuestion]):
    model = ExamQuestion

    async def add_all(self, questions: list[ExamQuestion]) -> None:
        """批量落题并一次 flush（建考时按序写 ``sort_order``）。"""
        for question in questions:
            self.db.add(question)
        await self.flush()

class ExamAttemptRepository(AsyncRepository[ExamAttempt]):
    model = ExamAttempt

    async def lock(self, attempt_id: uuid.UUID) -> ExamAttempt | None:
        """交卷/阅卷对 attempt 加行锁，串行化重复提交与重复发证。"""
        return (
            await self.db.execute(
                select(ExamAttempt)
                .where(ExamAttempt.id == attempt_id)
                .with_for_update()
            )
        ).scalars().first()

    async def get_active(
        self,
        *,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
        is_trial: bool = False,
        trial_id: uuid.UUID | None = None,
    ) -> ExamAttempt | None:
        conditions: list[Any] = [
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.user_id == user_id,
            ExamAttempt.status == "in_progress",
            ExamAttempt.is_trial.is_(is_trial),
        ]
        if is_trial:
            conditions.append(ExamAttempt.trial_id == trial_id)
        return await self.get_one(
            *conditions, order_by=ExamAttempt.started_at.desc()
        )

    async def count_used_attempts(
        self,
        *,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
        period_start: datetime.datetime,
    ) -> int:
        """周期内已消耗次数：expired 也计入（评审补充决定 2）。"""
        return await self.count(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.user_id == user_id,
            ExamAttempt.is_trial.is_(False),
            ExamAttempt.started_at >= period_start,
            ExamAttempt.status != "in_progress",
        )

    async def list_expired_candidates(self, limit: int = 200) -> list[ExamAttempt]:
        return await self.get_many(
            ExamAttempt.status == "in_progress",
            ExamAttempt.deadline_at.is_not(None),
            ExamAttempt.deadline_at < now_iso(),
            order_by=ExamAttempt.deadline_at,
            limit=limit,
        )

    async def list_admin(
        self,
        *,
        exam_id: uuid.UUID | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ExamAttempt], int]:
        conditions: list[Any] = []
        if exam_id is not None:
            conditions.append(ExamAttempt.exam_id == exam_id)
        if status is not None:
            conditions.append(ExamAttempt.status == status)
        total = await self.count(*conditions)
        rows = await self.get_many(
            *conditions,
            order_by=ExamAttempt.started_at.desc(),
            offset=offset,
            limit=limit,
        )
        return rows, total

    async def list_final_for_exam(self, exam_id: uuid.UUID) -> list[ExamAttempt]:
        """竞赛榜单候选：已最终化、非 trial 的 attempt。"""
        return await self.get_many(
            ExamAttempt.exam_id == exam_id,
            ExamAttempt.status.in_(("passed", "failed")),
            ExamAttempt.is_trial.is_(False),
            order_by=(ExamAttempt.score.desc(), ExamAttempt.submitted_at.asc()),
        )


class ExamAttemptAnswerRepository(AsyncRepository[ExamAttemptAnswer]):
    model = ExamAttemptAnswer

    async def list_for_attempt(self, attempt_id: uuid.UUID) -> list[ExamAttemptAnswer]:
        return await self.get_many(
            ExamAttemptAnswer.attempt_id == attempt_id,
            order_by=ExamAttemptAnswer.id,
        )

    async def get_by_question(
        self, attempt_id: uuid.UUID, question_id: uuid.UUID
    ) -> ExamAttemptAnswer | None:
        return await self.get_one(
            ExamAttemptAnswer.attempt_id == attempt_id,
            ExamAttemptAnswer.question_id == question_id,
        )


class ExamWrongAnswerRepository(AsyncRepository[ExamWrongAnswer]):
    model = ExamWrongAnswer

    async def get_for_user(
        self, user_id: uuid.UUID, question_id: uuid.UUID
    ) -> ExamWrongAnswer | None:
        return await self.get_one(
            ExamWrongAnswer.user_id == user_id,
            ExamWrongAnswer.question_id == question_id,
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[ExamWrongAnswer]:
        return await self.get_many(
            ExamWrongAnswer.user_id == user_id,
            order_by=(ExamWrongAnswer.last_wrong_at.desc(), ExamWrongAnswer.id.desc()),
        )


class ExamRegistrationRepository(AsyncRepository[ExamRegistration]):
    model = ExamRegistration

    async def get_for_user(
        self, exam_id: uuid.UUID, user_id: uuid.UUID
    ) -> ExamRegistration | None:
        return await self.get_one(
            ExamRegistration.exam_id == exam_id,
            ExamRegistration.user_id == user_id,
        )

    async def list_for_user(
        self, user_id: uuid.UUID
    ) -> list[tuple[ExamRegistration, str | None]]:
        rows = (
            await self.db.execute(
                select(ExamRegistration, Exam.title)
                .join(Exam, Exam.id == ExamRegistration.exam_id)
                .where(ExamRegistration.user_id == user_id)
                .order_by(ExamRegistration.registered_at.desc())
            )
        ).all()
        return [(reg, title) for reg, title in rows]

    async def count_registered(self, exam_id: uuid.UUID) -> int:
        return await self.count(
            ExamRegistration.exam_id == exam_id,
            ExamRegistration.status == "registered",
        )


class ExamTrialRepository(AsyncRepository[ExamTrial]):
    model = ExamTrial

    async def get_by_key(self, key: str) -> ExamTrial | None:
        return await self.get_one(ExamTrial.key == key)


class ExamPrerequisiteRepository(AsyncRepository[ExamPrerequisite]):
    model = ExamPrerequisite

    async def get_for_exam(self, exam_id: uuid.UUID) -> ExamPrerequisite | None:
        return await self.get_one(ExamPrerequisite.exam_id == exam_id)


class ExamCertificateRepository(AsyncRepository[ExamCertificate]):
    model = ExamCertificate

    async def passed_exists(self, *, exam_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """该用户是否已通过这场考试（防重复解锁/重复交卷）。"""
        return await self.exists(
            ExamCertificate.exam_id == exam_id,
            ExamCertificate.user_id == user_id,
            ExamCertificate.passed.is_(True),
        )

    async def get_passed(
        self, *, exam_id: uuid.UUID, user_id: uuid.UUID, for_update: bool = False
    ) -> ExamCertificate | None:
        stmt = select(ExamCertificate).where(
            ExamCertificate.exam_id == exam_id,
            ExamCertificate.user_id == user_id,
            ExamCertificate.passed.is_(True),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalars().first()

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
