"""判分：单选/判断/多选/填空自动判分、大小写与空白、顺序无关、快照保护。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam import bank_service
from app.modules.exam.models import ExamAttempt
from app.modules.exam.schemas import (
    ExamCreate,
    GroupCreate,
    QuestionCreate,
    QuestionUpdate,
    SubmitAnswersRequest,
)
from app.modules.exam.service import create_exam_ex, start_attempt, submit_attempt


async def _build_exam(db: AsyncSession, pass_score: int = 40) -> uuid.UUID:
    group = await bank_service.create_group(
        db, GroupCreate(key=f"grading-{uuid.uuid4().hex[:8]}", title="判分题组")
    )
    for q in (
        QuestionCreate(
            kind="single",
            content="单选",
            options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
            answer="A",
            score=10,
        ),
        QuestionCreate(kind="judge", content="判断", answer="T", score=10),
        QuestionCreate(
            kind="multiple",
            content="多选",
            options=[
                {"key": "A", "text": "a"},
                {"key": "B", "text": "b"},
                {"key": "C", "text": "c"},
            ],
            answer_data={"keys": ["A", "C"]},
            score=10,
        ),
        QuestionCreate(
            kind="fill",
            content="填空",
            answer_data={"text": "Hello World"},
            score=10,
        ),
    ):
        await bank_service.create_question(db, q.model_copy(update={"group_id": group.id}))

    exam = await create_exam_ex(
        db,
        ExamCreate(title="判分卷", pass_score=pass_score, group_ids=[group.id]),
    )
    await bank_service.publish_exam(db, exam.id)
    return exam.id


def _answers(questions, *, single="A", judge="T", multiple=None, fill="Hello World"):
    out = {}
    for q in questions:
        if q.kind == "single":
            out[q.id] = single
        elif q.kind == "judge":
            out[q.id] = judge
        elif q.kind == "multiple":
            out[q.id] = multiple if multiple is not None else ["A", "C"]
        elif q.kind == "fill":
            out[q.id] = fill
    return out


class TestAutoGrading:
    async def test_all_kinds_with_case_and_whitespace(self, db: AsyncSession):
        exam_id = await _build_exam(db)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)
        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(
                answers=_answers(
                    start.questions,
                    single="  a ",
                    judge=" t ",
                    multiple=["c", "A"],
                    fill="  hello world  ",
                )
            ),
        )
        assert result.passed is True
        assert result.score == 40
        assert result.status == "passed"

    async def test_multiple_order_does_not_matter(self, db: AsyncSession):
        exam_id = await _build_exam(db)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)
        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(
                answers=_answers(start.questions, multiple=["C", "A"])
            ),
        )
        assert result.passed is True
        # 多选少选/多选均错
        user2 = uuid.uuid4()
        start2 = await start_attempt(db, exam_id, user2)
        result2 = await submit_attempt(
            db,
            start2.attempt_id,
            user2,
            SubmitAnswersRequest(
                answers=_answers(start2.questions, multiple=["A"])
            ),
        )
        assert result2.score == 30
        assert result2.passed is False

    async def test_fill_contains_rule(self, db: AsyncSession):
        group = await bank_service.create_group(
            db, GroupCreate(key="fill-match", title="填空规则")
        )
        q = await bank_service.create_question(
            db,
            QuestionCreate(
                kind="fill",
                content="写一句问候",
                answer_data={"text": "hello"},
                config={"match": "contains"},
                score=10,
                group_id=group.id,
            ),
        )
        assert q.id
        exam = await create_exam_ex(
            db,
            ExamCreate(title="填空题", pass_score=10, group_ids=[group.id]),
        )
        await bank_service.publish_exam(db, exam.id)
        user = uuid.uuid4()
        start = await start_attempt(db, exam.id, user)
        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(
                answers={start.questions[0].id: "Say HELLO to you"}
            ),
        )
        assert result.passed is True

    async def test_snapshot_protects_inflight_attempt(self, db: AsyncSession):
        exam_id = await _build_exam(db)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)

        # 开考后改题：正确答案从 A 改为 B，分值改为 1。
        single_q = next(q for q in start.questions if q.kind == "single")
        await bank_service.update_question(
            db, single_q.id, QuestionUpdate(answer="B", score=1)
        )

        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers=_answers(start.questions)),
        )
        # 仍按开考快照判分（A 正确、10 分），不受改题影响。
        assert result.passed is True
        assert result.score == 40

    async def test_legacy_attempt_without_snapshot_falls_back(self, db: AsyncSession):
        exam_id = await _build_exam(db)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)
        attempt = await db.get(ExamAttempt, start.attempt_id)
        assert attempt is not None
        attempt.snapshot = None  # 旧 attempt 无快照
        await db.flush()

        result = await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers=_answers(start.questions)),
        )
        assert result.passed is True
        assert result.score == 40
