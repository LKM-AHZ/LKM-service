"""后台举报队列的写入实现（``core.ports.reports`` 的真实去处）。

写入与去重口径只在这里维护：字段默认值（``id`` / ``created_at``）与「同人同目标不重复
待审」的判定都依赖 ``Report`` 表的定义，放在 admin 域内才不会随调用方漂移。

匿名举报（``reporter_id is None``）**不做去重**：树洞访客没有身份，无法判断是否同一人；
历史行为如此，且去重会让「同一内容被多人匿名举报」只留一条。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Report


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
    """写入一条待审举报；已存在同人同目标的 pending 记录时返回 False。"""
    if reporter_id is not None:
        already = await db.scalar(
            select(Report.id).where(
                Report.reporter_id == reporter_id,
                Report.type == type,
                Report.target_id == target_id,
                Report.status == "pending",
            )
        )
        if already is not None:
            return False

    fields: dict[str, Any] = {
        "type": type,
        "target_id": target_id,
        "target_title": target_title,
        "reporter_id": reporter_id,
        "reporter_name": reporter_name,
        "reason": reason,
        "status": "pending",
    }
    if created_at is not None:
        fields["created_at"] = created_at
    # 树洞路径历史上显式传 uuid4（其用例跑在无 uuid7 服务端默认的库上）；
    # content 路径依赖 PG 服务端 uuid7 以保持 id 的时间序。两种都原样保留。
    if explicit_id is not None:
        fields["id"] = explicit_id

    db.add(Report(**fields))
    await db.flush()
    return True
