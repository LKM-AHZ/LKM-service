"""
审核引擎（端口）：内容降权 / 隐藏的规则评估入口。

feed 在时间线里对每条内容跑一次规则评估，原先直接
``from app.modules.admin.moderation.engine import ModerationResult, evaluate, load_active_rules``。
规则表（``moderation_rules``）与评估口径属 admin 域，feed 只需要「文本 + 规则 → 结果」。

``ModerationResult`` 在此以结构协议重新声明：feed 只读 ``penalty`` / ``should_hide``
两个字段，协议保证这两点，admin 的实现无需反向依赖 core 的数据类。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


class ModerationResult(Protocol):
    """规则评估结果（结构协议：与 admin 域的实现同形）。"""

    penalty: float  # 0..1 降权系数
    should_hide: bool  # 命中 hide 规则


async def load_active_rules(db: AsyncSession) -> list[Any]:
    """取当前生效的规则集（缓存/DB 由实现决定）。"""
    return await get("moderation_engine").load_active_rules(db)


def evaluate(text: str, rules: list[Any]) -> ModerationResult:
    """按规则集评估一段文本（纯函数，不碰 DB）。"""
    return get("moderation_engine").evaluate(text, rules)
