"""练习/错题本/trial：不产证书积分、错题 upsert/连对/掌握、答案不泄露、赛后题库。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam import bank_service
from app.modules.exam import service as exam_service
from app.modules.exam.models import ExamCertificate, ExamQuestionGroup
from app.modules.exam.schemas import (
    ExamCreate,
    GroupCreate,
    PracticeStartRequest,
    QuestionCreate,
    SubmitAnswersRequest,
    TrialCreate,
)
from app.modules.exam.service import (
    create_exam_ex,
    get_trial,
    list_practice_groups,
    list_wrong_answers,
    master_wrong_answer,
    remove_wrong_answer,
    start_practice,
    start_trial,
    submit_attempt,
)


async def _source_exam(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    group = await bank_service.create_group(
        db, GroupCreate(key=f"practice-{uuid.uuid4().hex[:8]}", title="练习题组")
    )
    for idx in range(2):
        await bank_service.create_question(
            db,
            QuestionCreate(
                kind="single",
                content=f"练习 {idx}",
                options=[{"key": "A", "text": "对"}, {"key": "B", "text": "错"}],
                answer="A",
                score=10,
                group_id=group.id,
            ),
        )
    comp = await create_exam_ex(
        db,
        ExamCreate(
            type="competition",
            title="竞赛卷",
            pass_score=20,
            group_ids=[group.id],
        ),
    )
    await bank_service.publish_exam(db, comp.id)
    return comp.id, group.id


async def _practice_exam(
    db: AsyncSession, comp_id: uuid.UUID
) -> uuid.UUID:
    practice = await bank_service.publish_practice(db, comp_id)
    await bank_service.publish_exam(db, practice.id)
    return practice.id


def _answer_all(questions, right: bool = True):
    return {q.id: ("A" if right else "B") for q in questions}


class TestPractice:
    async def test_practice_attempt_has_no_cert_or_points(
        self, db: AsyncSession, monkeypatch
    ):
        calls: list[str] = []

        async def _record(_db, _user_id, _event, ref_id):
            calls.append(ref_id)

        monkeypatch.setattr(exam_service.points_port, "enqueue_event", _record)

        comp_id, _group_id = await _source_exam(db)
        practice_id = await _practice_exam(db, comp_id)
        practice = await exam_service.ExamRepository(db).get_or_raise(
            practice_id, exam_service.ExamErr.EXAM_NOT_FOUND
        )
        assert practice.type == "practice"

        user = uuid.uuid4()
        start = await start_practice(
            db, user, PracticeStartRequest(exam_id=practice_id)
        )
        assert start.mode == "practice"
        # 考生侧 DTO 不泄露答案/解析。
        for q in start.questions:
            assert not hasattr(q, "answer")
            assert not hasattr(q, "answer_data")
            assert not hasattr(q, "analysis")

        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers=_answer_all(start.questions)),
        )
        assert result.passed is True
        assert result.certificate_id is None
        certs = await db.scalar(select(func.count()).select_from(ExamCertificate))
        assert certs == 0
        assert calls == []

    async def test_practice_groups_listing_and_shared_question(
        self, db: AsyncSession
    ):
        comp_id, group_id = await _source_exam(db)
        practice_id = await _practice_exam(db, comp_id)

        groups = await list_practice_groups(db)
        assert len(groups) == 1
        assert groups[0].id == group_id
        assert groups[0].question_count == 2
        assert practice_id in groups[0].exam_ids

        # publish-practice 不复制题目：题组仍是同一行。
        q_groups = (await db.execute(select(ExamQuestionGroup))).scalars().all()
        assert len(q_groups) == 1

    async def test_wrong_book_upsert_streak_master_and_remove(
        self, db: AsyncSession
    ):
        comp_id, _ = await _source_exam(db)
        practice_id = await _practice_exam(db, comp_id)
        user = uuid.uuid4()

        wrong_q = None
        for _ in range(4):
            start = await start_practice(
                db, user, PracticeStartRequest(exam_id=practice_id)
            )
            first, second = start.questions
            wrong_q = first
            answers = {
                first.id: ("B" if _ == 0 else "A"),
                second.id: "A",
            }
            await submit_attempt(
                db, start.attempt_id, user, SubmitAnswersRequest(answers=answers)
            )

        rows = await list_wrong_answers(db, user)
        assert len(rows) == 1
        assert rows[0].question_id == wrong_q.id
        assert rows[0].wrong_count == 1
        assert rows[0].correct_streak == 3
        assert rows[0].mastered is True

        mastered = await master_wrong_answer(db, user, wrong_q.id)
        assert mastered.mastered is True
        await remove_wrong_answer(db, user, wrong_q.id)
        assert await list_wrong_answers(db, user) == []

    async def test_wrong_only_filters_questions(self, db: AsyncSession):
        comp_id, _ = await _source_exam(db)
        practice_id = await _practice_exam(db, comp_id)
        user = uuid.uuid4()
        start = await start_practice(
            db, user, PracticeStartRequest(exam_id=practice_id)
        )
        first, second = start.questions
        await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers={first.id: "B", second.id: "A"}),
        )

        retry = await start_practice(
            db, user, PracticeStartRequest(exam_id=practice_id, wrong_only=True)
        )
        assert [q.id for q in retry.questions] == [first.id]

    async def test_deleted_question_shows_placeholder_in_wrong_book(
        self, db: AsyncSession
    ):
        comp_id, _ = await _source_exam(db)
        practice_id = await _practice_exam(db, comp_id)
        user = uuid.uuid4()
        start = await start_practice(
            db, user, PracticeStartRequest(exam_id=practice_id)
        )
        first, second = start.questions
        await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers={first.id: "B", second.id: "A"}),
        )
        await bank_service.delete_question(db, first.id)

        rows = await list_wrong_answers(db, user)
        assert rows[0].unavailable is True
        assert rows[0].content == "该题已下架"
        assert rows[0].options == []

    async def test_trial_attempt_is_not_certified(self, db: AsyncSession):
        comp_id, _ = await _source_exam(db)
        trial = await bank_service.create_trial(
            db,
            comp_id,
            TrialCreate(key="share-1", title="分享测评"),
        )
        meta = await get_trial(db, "share-1")
        assert meta.title == "分享测评" and meta.is_published is True

        user = uuid.uuid4()
        start = await start_trial(db, trial.key, user)
        assert start.is_trial is True
        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers=_answer_all(start.questions)),
        )
        assert result.passed is True
        certs = await db.scalar(select(func.count()).select_from(ExamCertificate))
        assert certs == 0
