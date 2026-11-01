"""exam 域对外只读 API（``core.ports.exam`` 的实现去处）。

content 域在发帖时判定「是否持有初级通识认证」，此前直接读 ``exam.models`` 的两张表。
判定口径（证书表 + 考试类型的 join 条件）属 exam 域，放在这里才不会两边漂移。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.exam.models import Exam, ExamCertificate


async def has_normal_certification(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """用户是否持有初级通识认证证书（type=exam 且 unlock_level=normal）。"""
    stmt = (
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
    return (await db.scalar(stmt)) is not None
