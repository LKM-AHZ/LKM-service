"""题库：题组 CRUD、题目复用、审核状态、软删除、导入导出与非法答案校验。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam import bank_service
from app.modules.exam.errors import ExamErr
from app.modules.exam.models import ExamQuestion
from app.modules.exam.schemas import (
    ExamCreate,
    GroupCreate,
    GroupUpdate,
    QuestionCreate,
    QuestionUpdate,
)
from app.modules.exam.service import create_exam_ex, start_attempt
from core.err import BizError
from tests.conftest import auth_user_uid


def _single(answer: str = "A", score: int = 10, **kw) -> QuestionCreate:
    return QuestionCreate(
        kind="single",
        content=kw.pop("content", "1+1=?"),
        options=[{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
        answer=answer,
        score=score,
        **kw,
    )


async def _group(db: AsyncSession, key: str = "g1") -> uuid.UUID:
    group = await bank_service.create_group(
        db, GroupCreate(key=key, title="基础题组")
    )
    return group.id


async def _exam_with_group(
    db: AsyncSession,
    group_id: uuid.UUID,
    *,
    title: str = "题库卷",
    pass_score: int = 10,
) -> uuid.UUID:
    exam = await create_exam_ex(
        db,
        ExamCreate(
            title=title,
            type="exam",
            pass_score=pass_score,
            group_ids=[group_id],
        ),
    )
    return exam.id


async def _publish(db: AsyncSession, exam_id: uuid.UUID) -> None:
    await bank_service.publish_exam(db, exam_id)


class TestQuestionBank:
    async def test_group_crud_and_key_conflict(self, db: AsyncSession):
        group = await bank_service.create_group(
            db, GroupCreate(key="basics", title="基础")
        )
        assert group.key == "basics"
        with pytest.raises(BizError) as exc:
            await bank_service.create_group(db, GroupCreate(key="basics", title="重复"))
        assert exc.value.errcode == ExamErr.GROUP_KEY_TAKEN

        updated = await bank_service.update_group(
            db, group.id, GroupUpdate(title="基础（改）")
        )
        assert updated.title == "基础（改）"
        assert updated.question_count == 0

        await bank_service.delete_group(db, group.id)
        items, total = await bank_service.list_groups(db)
        assert total == 0 and items == []

    async def test_question_reused_across_exams(self, db: AsyncSession):
        group_id = await _group(db)
        await bank_service.create_question(db, _single(group_id=group_id))

        exam_a = await _exam_with_group(db, group_id, title="A", pass_score=10)
        exam_b = await _exam_with_group(db, group_id, title="B", pass_score=10)
        await _publish(db, exam_a)
        await _publish(db, exam_b)

        start_a = await start_attempt(db, exam_a, uuid.uuid4())
        start_b = await start_attempt(db, exam_b, uuid.uuid4())
        assert start_a.questions[0].id == start_b.questions[0].id

        rows = (await db.execute(select(ExamQuestion))).scalars().all()
        assert len(rows) == 1  # 题目只有一行，两张卷共享

    async def test_review_status_gates_attempt(self, db: AsyncSession):
        group_id = await _group(db)
        draft = await bank_service.create_question(
            db, _single(group_id=group_id, review_status="draft")
        )
        await bank_service.create_question(db, _single(group_id=group_id))
        exam_id = await _exam_with_group(db, group_id)
        await _publish(db, exam_id)

        # 未审核题目不进入开考快照；已审核题目正常发放。
        start = await start_attempt(db, exam_id, uuid.uuid4())
        assert len(start.questions) == 1

        approved = await bank_service.review_question(db, draft.id, "approve")
        assert approved.review_status == "approved"
        start2 = await start_attempt(db, exam_id, uuid.uuid4())
        assert len(start2.questions) == 2

    async def test_soft_deleted_question_excluded_but_row_kept(
        self, db: AsyncSession
    ):
        group_id = await _group(db)
        q = await bank_service.create_question(db, _single(group_id=group_id))
        await bank_service.delete_question(db, q.id)

        items, total = await bank_service.list_questions(
            db, group_id=group_id, include_deleted=False
        )
        assert total == 0 and items == []
        items, total = await bank_service.list_questions(
            db, group_id=group_id, include_deleted=True
        )
        assert total == 1 and items[0].deleted_at is not None

        row = (
            await db.execute(select(ExamQuestion).where(ExamQuestion.id == q.id))
        ).scalars().one()
        assert row.deleted_at is not None  # 行仍在，快照/错题引用不悬空

    async def test_invalid_answers_rejected(self):
        with pytest.raises(ValidationError):
            QuestionCreate(
                kind="single", content="x", options=[], answer="A"
            )
        with pytest.raises(ValidationError):
            QuestionCreate(
                kind="multiple",
                content="x",
                options=[{"key": "A", "text": "a"}],
                answer_data={"keys": ["B"]},
            )
        with pytest.raises(ValidationError):
            QuestionCreate(kind="fill", content="x", answer_data={})

        text = QuestionCreate(
            kind="text", content="论述", grading_mode="auto", answer_data={}
        )
        assert text.resolved_grading_mode() == "manual"

    async def test_import_export_and_update(self, db: AsyncSession):
        group_id = await _group(db)
        imported = await bank_service.import_questions(
            db,
            group_id,
            [
                QuestionCreate(
                    kind="fill",
                    content="国家首都？",
                    answer_data={"text": "北京"},
                    score=5,
                ),
                QuestionCreate(
                    kind="multiple",
                    content="偶数？",
                    options=[
                        {"key": "A", "text": "2"},
                        {"key": "B", "text": "3"},
                        {"key": "C", "text": "4"},
                    ],
                    answer_data={"keys": ["A", "C"]},
                    score=5,
                ),
            ],
        )
        assert imported == 2
        exported = await bank_service.export_questions(db, group_id=group_id)
        assert {q.kind for q in exported} == {"fill", "multiple"}
        assert all(q.answer_data for q in exported)  # 管理端导出含答案

        upd = await bank_service.update_question(
            db, exported[0].id, QuestionUpdate(score=8)
        )
        assert upd.score == 8

    async def test_admin_route_requires_admin(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        user = await auth_user_uid(
            auth_db, username="normal1", account_level="normal", with_token=True
        )
        resp = await client.get(
            "/api/v1/exam/admin/groups",
            headers={"Authorization": f"Bearer {user.token}"},
        )
        assert resp.status_code == 403
