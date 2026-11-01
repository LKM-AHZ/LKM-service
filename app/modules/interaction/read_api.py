"""interaction 域对外只读 API（``core.ports.interaction`` 的实现去处）。

只提供判定与聚合谓词：调用方传入自己的外键列做关联，interaction 域只用自己的表。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.modules.interaction.models import InteractionFavorite


def favorite_count_subquery(outer_column: Any) -> Any:
    """「该内容被收藏次数」的标量子查询（``outer_column`` 由调用方传入）。

    返回的是**未 label** 的标量表达式，由调用方按需命名——SQL 与原先内联写法逐字相同。
    """
    return (
        select(func.count())
        .select_from(InteractionFavorite)
        .where(InteractionFavorite.content_id == outer_column)
        .scalar_subquery()
    )
