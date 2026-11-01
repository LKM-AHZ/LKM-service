"""exam 域只读端口：认证资格判定。

content 发帖时据此决定是否需要认证，此前直接读 ``exam.models``。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def has_normal_certification(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """用户是否持有初级通识认证证书（type=exam 且 unlock_level=normal）。"""
    return await get("exam_read").has_normal_certification(db, user_id)
