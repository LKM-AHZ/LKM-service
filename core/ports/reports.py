"""
后台举报队列的写入口（端口）。

content（帖子/评论）与 treehole（匿名树洞）把举报投进**同一个**后台审核队列，原先各自
直接构造 ``admin.models.Report``。跨域直接构造对方 ORM 行意味着：字段增删、默认值、
去重口径都得两边同时改，改漏一处就是脏数据而不是报错。

改为端口后，写入口径（含「同人同目标不重复待审」的去重）由 admin 域唯一持有。

``reporter_id is None``（匿名树洞）不做去重——匿名举报无法区分同一访客，历史行为如此。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def create_if_absent(
    db: AsyncSession,
    *,
    type: str,
    target_id: str,
    target_title: str,
    reporter_id: uuid.UUID | None,
    reporter_name: str,
    reason: str,
    created_at: str | None = None,
    explicit_id: uuid.UUID | None = None,
) -> bool:
    """写入后台举报队列；已存在同人同目标的 pending 记录时返回 False。"""
    return await get("reports_writer").create_if_absent(
        db,
        type=type,
        target_id=target_id,
        target_title=target_title,
        reporter_id=reporter_id,
        reporter_name=reporter_name,
        reason=reason,
        created_at=created_at,
        explicit_id=explicit_id,
    )
