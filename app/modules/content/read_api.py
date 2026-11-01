"""content 域对外只读 API（``core.ports.content`` 的实现去处）。

只返回**不可变的摘要对象**，不把 ORM 实例交给调用方：跨域持有可变行会让「谁负责 flush」
变得模糊，而调用方（通知、后台）只需要几个字段。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import (
    ContentComment,
    ContentItem,
    QAAnswer,
    QAQuestion,
)


@dataclass(frozen=True)
class ItemBrief:
    id: uuid.UUID
    title: str
    author_id: uuid.UUID | None


@dataclass(frozen=True)
class CommentBrief:
    id: uuid.UUID
    content_id: uuid.UUID
    user_id: uuid.UUID
    parent_id: uuid.UUID | None


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


async def item_brief(db: AsyncSession, item_id: uuid.UUID) -> ItemBrief | None:
    """未软删的内容项摘要。"""
    row = await db.scalar(
        select(ContentItem).where(
            ContentItem.id == item_id, ContentItem.deleted_at.is_(None)
        )
    )
    if row is None:
        return None
    return ItemBrief(id=row.id, title=row.title, author_id=row.author_id)


async def comment_brief(
    db: AsyncSession, comment_id: uuid.UUID
) -> CommentBrief | None:
    """未软删的评论摘要。"""
    row = await db.scalar(
        select(ContentComment).where(
            ContentComment.id == comment_id, ContentComment.deleted_at.is_(None)
        )
    )
    if row is None:
        return None
    return CommentBrief(
        id=row.id, content_id=row.content_id, user_id=row.user_id, parent_id=row.parent_id
    )


async def qa_answer_brief(
    db: AsyncSession, answer_id: uuid.UUID
) -> QaAnswerBrief | None:
    """问答回答摘要。"""
    row = await db.scalar(select(QAAnswer).where(QAAnswer.id == answer_id))
    if row is None:
        return None
    return QaAnswerBrief(id=row.id, question_id=row.question_id, author_id=row.author_id)


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
