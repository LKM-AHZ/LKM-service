"""qa 域端口：问答的只读摘要与时间线源。

步骤 9 切片 1 把问答从 content 拆出来后，原先经 ``content_read`` 暴露的两项能力归还属主：
通知解析「采纳提醒」要回答摘要，feed 要问答源查询。两者都只读、都同事务执行。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def qa_answer_brief(db: AsyncSession, answer_id: uuid.UUID) -> Any:
    """问答回答摘要（``id`` / ``question_id`` / ``author_id``）；不存在返回 None。"""
    return await get("qa_read").qa_answer_brief(db, answer_id)


async def qa_question_brief(db: AsyncSession, question_id: uuid.UUID) -> Any:
    """问答问题摘要（``id`` / ``title`` / ``bounty_per_person``）；不存在返回 None。"""
    return await get("qa_read").qa_question_brief(db, question_id)


def select_feed_source(
    *,
    author_ids: Any,
    board_ids: Any,
    before_time: Any,
    before_id: Any,
    after_time: Any,
    after_id: Any,
    limit: int,
) -> Any:
    """open/accepted 问答的按游标一窗（未执行）。"""
    return get("qa_read").select_feed_source(
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )
