"""人工阅卷：待审状态、逐题评分、最终化、重复阅卷不重复发证/积分、权限。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam import bank_service
from app.modules.exam import service as exam_service
from app.modules.exam.errors import ExamErr
from app.modules.exam.models import ExamCertificate
from app.modules.exam.schemas import (
    ExamCreate,
    GroupCreate,
    QuestionCreate,
    ReviewAnswerRequest,
    SubmitAnswersRequest,
)
from app.modules.exam.service import (
    create_exam_ex,
    get_attempt_result,
    review_answer,
    start_attempt,
    submit_attempt,
)
from core.err import BizError
from tests.conftest import auth_user_uid


async def _manual_exam(
    db: AsyncSession, *, text_count: int = 1, pass_score: int = 10
) -> uuid.UUID:
    group = await bank_service.create_group(
        db, GroupCreate(key=f"review-{uuid.uuid4().hex[:8]}", title="阅卷题组")
    )
    await bank_service.create_question(
        db,
        QuestionCreate(
            kind="single",
            content="单选",
            options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
            answer="A",
            score=10,
            group_id=group.id,
        ),
    )
    for idx in range(text_count):
        await bank_service.create_question(
            db,
            QuestionCreate(
                kind="text",
                content=f"论述题 {idx}",
                answer_data={"reference": "参考要点"},
                analysis="解析",
                score=10,
                group_id=group.id,
            ),
        )
    exam = await create_exam_ex(
        db,
        ExamCreate(title="阅卷卷", pass_score=pass_score, group_ids=[group.id]),
    )
    await bank_service.publish_exam(db, exam.id)
    return exam.id


async def _submit_pending(db: AsyncSession, exam_id: uuid.UUID, user: uuid.UUID):
    start = await start_attempt(db, exam_id, user)
    result = await submit_attempt(
        db,
        start.attempt_id,
        user,
        SubmitAnswersRequest(
            answers={
                q.id: ("A" if q.kind == "single" else "我的论述") for q in start.questions
            }
        ),
    )
    return start, result


class TestManualReview:
    async def test_subjective_goes_pending_then_passes(self, db: AsyncSession):
        exam_id = await _manual_exam(db)
        user = uuid.uuid4()
        start, result = await _submit_pending(db, exam_id, user)
        assert result.status == "pending_review"
        assert result.passed is False
        assert result.certificate_id is None

        certs = await db.scalar(
            select(func.count()).select_from(ExamCertificate)
        )
        assert certs == 0

        text_q = next(q for q in start.questions if q.kind == "text")
        reviewed = await review_answer(
            db,
            start.attempt_id,
            text_q.id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=True, awarded_points=10, note="要点齐全"),
        )
        assert reviewed.status == "passed"
        assert reviewed.score == 20
        assert reviewed.passed is True
        certs = await db.scalar(
            select(func.count()).select_from(ExamCertificate)
        )
        assert certs == 1

    async def test_finalize_only_after_all_manual_answered(self, db: AsyncSession):
        exam_id = await _manual_exam(db, text_count=2)
        user = uuid.uuid4()
        start, result = await _submit_pending(db, exam_id, user)
        assert result.status == "pending_review"

        texts = [q for q in start.questions if q.kind == "text"]
        one = await review_answer(
            db,
            start.attempt_id,
            texts[0].id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=True, awarded_points=10),
        )
        assert one.status == "pending_review"

        two = await review_answer(
            db,
            start.attempt_id,
            texts[1].id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=True, awarded_points=10),
        )
        assert two.status == "passed"
        assert two.score == 30

    async def test_only_manual_questions_reviewable(self, db: AsyncSession):
        exam_id = await _manual_exam(db)
        user = uuid.uuid4()
        start, _ = await _submit_pending(db, exam_id, user)
        single = next(q for q in start.questions if q.kind == "single")
        with pytest.raises(BizError) as exc:
            await review_answer(
                db,
                start.attempt_id,
                single.id,
                uuid.uuid4(),
                ReviewAnswerRequest(is_correct=True),
            )
        assert exc.value.errcode == ExamErr.REVIEW_TARGET_NOT_MANUAL

    async def test_repeat_review_keeps_one_cert_and_one_points_event(
        self, db: AsyncSession, monkeypatch
    ):
        calls: list[str] = []

        async def _record(_db, _user_id, _event, ref_id):
            calls.append(ref_id)

        monkeypatch.setattr(exam_service.points_port, "enqueue_event", _record)

        exam_id = await _manual_exam(db, pass_score=10)
        user = uuid.uuid4()
        start, _ = await _submit_pending(db, exam_id, user)
        text_q = next(q for q in start.questions if q.kind == "text")

        first = await review_answer(
            db,
            start.attempt_id,
            text_q.id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=True, awarded_points=10),
        )
        assert first.status == "passed"
        certs = (await db.execute(select(ExamCertificate))).scalars().all()
        assert len(certs) == 1
        assert certs[0].score == 20
        assert len(calls) == 1

        # 修正为 0 分：证书分数只升不降，积分不重复入队。
        second = await review_answer(
            db,
            start.attempt_id,
            text_q.id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=False, awarded_points=0),
        )
        assert second.status == "passed"  # 10 >= pass_score
        certs = (await db.execute(select(ExamCertificate))).scalars().all()
        assert len(certs) == 1
        assert certs[0].score == 20
        assert len(calls) == 1

    async def test_result_hides_answers_until_final(self, db: AsyncSession):
        exam_id = await _manual_exam(db)
        user = uuid.uuid4()
        start, _ = await _submit_pending(db, exam_id, user)
        pending = await get_attempt_result(db, start.attempt_id, user)
        assert pending.status == "pending_review"
        assert all(q.correct_answer is None for q in pending.questions)
        assert all(q.analysis is None for q in pending.questions)

        text_q = next(q for q in start.questions if q.kind == "text")
        await review_answer(
            db,
            start.attempt_id,
            text_q.id,
            uuid.uuid4(),
            ReviewAnswerRequest(is_correct=True, awarded_points=10),
        )
        final = await get_attempt_result(db, start.attempt_id, user)
        assert final.status == "passed"
        single = next(q for q in final.questions if q.kind == "single")
        assert single.correct_answer == "A"

    async def test_non_admin_cannot_review_via_route(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        user = await auth_user_uid(
            auth_db, username="reviewer-no", account_level="normal", with_token=True
        )
        exam_id = await _manual_exam(db)
        start, _ = await _submit_pending(db, exam_id, user.id)
        text_q = next(q for q in start.questions if q.kind == "text")
        resp = await client.post(
            f"/api/v1/exam/admin/attempts/{start.attempt_id}/answers/{text_q.id}/review",
            json={"is_correct": True, "awarded_points": 10},
            headers={"Authorization": f"Bearer {user.token}"},
        )
        assert resp.status_code == 403
