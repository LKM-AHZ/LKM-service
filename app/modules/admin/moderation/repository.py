"""moderation 域的仓储子类：审校规则 CRUD 的 SQLAlchemy 表达式收口。

规则试跑/评估走 :mod:`app.modules.admin.moderation.engine`（不在此收编）；
本文件只负责 ``moderation_rules`` 的行级读写。
"""

from __future__ import annotations

from app.db.repository import AsyncRepository
from app.modules.admin.models import ModerationRule


class ModerationRuleRepository(AsyncRepository[ModerationRule]):
    model = ModerationRule

    async def list_ordered(self) -> list[ModerationRule]:
        """规则列表，按 id 升序（确定性展示序）。"""
        return await self.get_many(order_by=ModerationRule.id.asc())
