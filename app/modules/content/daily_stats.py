"""内容域按日统计：供跨域报表（运营日报）经 ``core.ports.content_stats`` 读取。

core 不得知道业务表，故这段读能力留在业务侧、由 ``app.bootstrap`` 绑定进端口。
查询保持原样（worker 池、只计未软删行）——从 ``app/flows/ops_daily_body`` 原样迁出，
行为零变化。
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select

from app.modules.content.models import ContentItem
from core.db.session import new_worker_session


async def count_content_created_by_day(days: int) -> dict[str, int]:
    """业务库近 N 天按日新增内容数（只计当前未软删的行）。

    用 **worker 池**（``new_worker_session``）——后台批处理不该与在线请求争连接。
    """
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    db = await new_worker_session()
    try:
        day = func.date_trunc("day", ContentItem.created_at).label("d")
        rows = (
            await db.execute(
                select(day, func.count())
                .where(
                    ContentItem.created_at >= since,
                    ContentItem.deleted_at.is_(None),
                )
                .group_by(day)
                .order_by(day)
            )
        ).all()
    finally:
        await db.close()
    return {row[0].date().isoformat(): int(row[1]) for row in rows}
