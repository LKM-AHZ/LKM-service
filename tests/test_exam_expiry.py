"""考试过期兜底扫描：只把超期 in_progress 置 expired，不影响未到期作答。"""

import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam.models import Exam, ExamAttempt
from app.modules.exam.service import expire_overdue_attempts
from core.db.base import now_iso


async def test_expire_overdue_attempts(db: AsyncSession) -> None:
    exam = Exam(title="过期扫描", type="exam", pass_score=1, time_limit_min=30)
    db.add(exam)
    await db.flush()

    overdue = ExamAttempt(
        exam_id=exam.id,
        user_id=uuid.uuid4(),
        status="in_progress",
        deadline_at=now_iso() - datetime.timedelta(hours=1),
    )
    alive = ExamAttempt(
        exam_id=exam.id,
        user_id=uuid.uuid4(),
        status="in_progress",
        deadline_at=now_iso() + datetime.timedelta(hours=1),
    )
    db.add_all([overdue, alive])
    await db.flush()

    assert await expire_overdue_attempts(db) == 1
    await db.flush()
    assert overdue.status == "expired"
    assert alive.status == "in_progress"
