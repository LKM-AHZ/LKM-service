"""问答（QA）域对外只读 API（``core.ports.qa`` 的实现去处）。

只返回不可变摘要，不把 ORM 实例交给调用方——通知任务只需要「回答属于哪个问题、问题
标题与人均悬赏」这几项。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.qa.models import QAAnswer, QAQuestion


@dataclass(frozen=True)
class QaAnswerBrief:
    id: uuid.UUID
    question_id: uuid.UUID
    author_id: uuid.UUID


@dataclass(frozen=True)
class QaQuestionBrief:
    id: uuid.UUID
    title: str
    bounty_per_person: int


async def qa_answer_brief(
    db: AsyncSession, answer_id: uuid.UUID
) -> QaAnswerBrief | None:
    """问答回答摘要。"""
    row = await db.scalar(select(QAAnswer).where(QAAnswer.id == answer_id))
    if row is None:
        return None
    return QaAnswerBrief(
        id=row.id, question_id=row.question_id, author_id=row.author_id
    )


async def qa_question_brief(
    db: AsyncSession, question_id: uuid.UUID
) -> QaQuestionBrief | None:
    """问答问题摘要（含悬赏单价）。"""
    row = await db.scalar(select(QAQuestion).where(QAQuestion.id == question_id))
    if row is None:
        return None
    return QaQuestionBrief(
        id=row.id, title=row.title, bounty_per_person=row.bounty_per_person
    )
