"""报名、前置门槛、次数限制与并发唯一性（证件/报名）。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.modules.exam import bank_service
from app.modules.exam.errors import ExamErr
from app.modules.exam.models import ExamCertificate, ExamRegistration
from app.modules.exam.schemas import (
    ExamCreate,
    GroupCreate,
    QuestionCreate,
    SubmitAnswersRequest,
)
from app.modules.exam.service import (
    cancel_registration,
    create_exam_ex,
    register_exam,
    start_attempt,
    submit_attempt,
)
from core.err import BizError
from tests.conftest import auth_user_uid


async def _exam(
    db: AsyncSession,
    *,
    requires_registration: bool = False,
    prerequisite_exam_id: uuid.UUID | None = None,
    required_level: str | None = None,
    max_attempts: int | None = None,
    title: str = "报名卷",
) -> uuid.UUID:
    group = await bank_service.create_group(
        db, GroupCreate(key=f"reg-{uuid.uuid4().hex[:8]}", title="报名题组")
    )
    await bank_service.create_question(
        db,
        QuestionCreate(
            kind="single",
            content="1+1",
            options=[{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            answer="A",
            score=10,
            group_id=group.id,
        ),
    )
    exam = await create_exam_ex(
        db,
        ExamCreate(
            title=title,
            pass_score=10,
            group_ids=[group.id],
            requires_registration=requires_registration,
            prerequisite_exam_id=prerequisite_exam_id,
            required_level=required_level,
            max_attempts=max_attempts,
        ),
    )
    await bank_service.publish_exam(db, exam.id)
    return exam.id


async def _pass_exam(db: AsyncSession, exam_id: uuid.UUID, user: uuid.UUID) -> None:
    start = await start_attempt(db, exam_id, user)
    await submit_attempt(
        db,
        start.attempt_id,
        user,
        SubmitAnswersRequest(answers={start.questions[0].id: "A"}),
    )


class TestRegistration:
    async def test_register_unique_cancel_and_revive(self, db: AsyncSession):
        exam_id = await _exam(db, requires_registration=True)
        user = uuid.uuid4()

        reg = await register_exam(db, exam_id, user)
        assert reg.status == "registered"
        with pytest.raises(BizError) as exc:
            await register_exam(db, exam_id, user)
        assert exc.value.errcode == ExamErr.ALREADY_REGISTERED

        cancelled = await cancel_registration(db, exam_id, user)
        assert cancelled.status == "cancelled"
        with pytest.raises(BizError):
            await cancel_registration(db, exam_id, user)

        revived = await register_exam(db, exam_id, user)
        assert revived.status == "registered"
        assert revived.cancelled_at is None

    async def test_registration_required_to_start(self, db: AsyncSession):
        exam_id = await _exam(db, requires_registration=True)
        user = uuid.uuid4()
        with pytest.raises(BizError) as exc:
            await start_attempt(db, exam_id, user)
        assert exc.value.errcode == ExamErr.NOT_REGISTERED

        await register_exam(db, exam_id, user)
        start = await start_attempt(db, exam_id, user)
        assert len(start.questions) == 1

    async def test_prerequisite_certificate_required(self, db: AsyncSession):
        base = await _exam(db, title="前置卷")
        user = uuid.uuid4()
        target = await _exam(db, prerequisite_exam_id=base, title="进阶卷")

        with pytest.raises(BizError) as exc:
            await start_attempt(db, target, user)
        assert exc.value.errcode == ExamErr.PREREQUISITE_NOT_MET

        await _pass_exam(db, base, user)
        start = await start_attempt(db, target, user)
        assert len(start.questions) == 1

    async def test_max_attempts_counts_expired_and_failed(self, db: AsyncSession):
        exam_id = await _exam(db, max_attempts=1)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)
        await submit_attempt(
            db,
            start.attempt_id,
            user,
            SubmitAnswersRequest(answers={start.questions[0].id: "B"}),
        )
        with pytest.raises(BizError) as exc:
            await start_attempt(db, exam_id, user)
        assert exc.value.errcode == ExamErr.MAX_ATTEMPTS_REACHED

    async def test_level_prerequisite_rejects_via_route(
        self, client, db, auth_db: AsyncSession, auth_seam_realm: None
    ):
        user = await auth_user_uid(
            auth_db, username="lvl", account_level="normal", with_token=True
        )
        exam_id = await _exam(db, required_level="admin", title="高级卷")
        resp = await client.post(
            f"/api/v1/exam/{exam_id}/attempts",
            headers={"Authorization": f"Bearer {user.token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == ExamErr.PREREQUISITE_NOT_MET

    async def test_concurrent_registration_single_row(self, db: AsyncSession):
        exam_id = await _exam(db, requires_registration=True)
        user = uuid.uuid4()
        await db.commit()

        assert isinstance(db.bind, AsyncEngine)
        engine = create_async_engine(db.bind.url, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with maker() as session:
                try:
                    await register_exam(session, exam_id, user)
                    await session.commit()
                    return True
                except BizError:
                    await session.rollback()
                    return False

        try:
            results = await asyncio.gather(attempt(), attempt())
        finally:
            await engine.dispose()
        assert sorted(results) == [False, True]
        count = await db.scalar(
            select(func.count())
            .select_from(ExamRegistration)
            .where(ExamRegistration.exam_id == exam_id)
        )
        assert count == 1

    async def test_concurrent_submit_single_certificate(self, db: AsyncSession):
        exam_id = await _exam(db)
        user = uuid.uuid4()
        start = await start_attempt(db, exam_id, user)
        answers = {start.questions[0].id: "A"}
        await db.commit()

        assert isinstance(db.bind, AsyncEngine)
        engine = create_async_engine(db.bind.url, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with maker() as session:
                try:
                    await submit_attempt(
                        session,
                        start.attempt_id,
                        user,
                        SubmitAnswersRequest(answers=answers),
                    )
                    await session.commit()
                    return True
                except BizError:
                    await session.rollback()
                    return False

        try:
            results = await asyncio.gather(attempt(), attempt())
        finally:
            await engine.dispose()
        assert sorted(results) == [False, True]
        certs = (
            await db.execute(
                select(ExamCertificate).where(
                    ExamCertificate.exam_id == exam_id,
                    ExamCertificate.user_id == user,
                )
            )
        ).scalars().all()
        assert len(certs) == 1
        assert certs[0].passed is True
