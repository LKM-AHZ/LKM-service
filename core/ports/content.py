"""content 域只读端口：通知与后台需要的「内容摘要」。

notification 解析行为事件时要回查内容/评论/问答的标题与作者，原先直接读
``content.models``。摘要形态由 content 域定义（``read_api`` 的数据类），调用方只读字段，
不持有 ORM 实例。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def item_brief(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """未软删的内容项摘要（``id`` / ``title`` / ``author_id``）；不存在返回 None。"""
    return await get("content_read").item_brief(db, item_id)


async def comment_brief(db: AsyncSession, comment_id: uuid.UUID) -> Any:
    """未软删的评论摘要（``id`` / ``content_id`` / ``user_id`` / ``parent_id``）。"""
    return await get("content_read").comment_brief(db, comment_id)


async def qa_answer_brief(db: AsyncSession, answer_id: uuid.UUID) -> Any:
    """问答回答摘要（``id`` / ``question_id`` / ``author_id``）。"""
    return await get("content_read").qa_answer_brief(db, answer_id)


async def qa_question_brief(db: AsyncSession, question_id: uuid.UUID) -> Any:
    """问答问题摘要（``id`` / ``title`` / ``bounty_per_person``）。"""
    return await get("content_read").qa_question_brief(db, question_id)
