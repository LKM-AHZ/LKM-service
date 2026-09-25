"""后台举报示例数据。用法：uv run python -m app.modules.admin.seed

为 reports 表填充示例举报（幂等），使后台「举报」页有可展示内容。
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import new_worker_session as new_session
from app.modules.admin.models import Report
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）

SEED_REPORTS: list[dict[str, str]] = [
    {
        "type": "post",
        "target_id": "post-101",
        "target_title": "某用户发布广告垃圾帖",
        "reporter_name": "七月O",
        "reason": "疑似营销推广，与板块主题无关。",
        "status": "pending",
    },
    {
        "type": "comment",
        "target_id": "comment-88",
        "target_title": "帖子下的恶意评论",
        "reporter_name": "七月花",
        "reason": "评论包含人身攻击内容。",
        "status": "pending",
    },
    {
        "type": "file",
        "target_id": "file-7",
        "target_title": "芯片设计入门教程.pdf",
        "reporter_name": "算法工坊",
        "reason": "文件疑似含版权内容。",
        "status": "resolved",
    },
]


async def seed_reports(db: AsyncSession) -> int:
    count = 0
    for data in SEED_REPORTS:
        # 幂等键取举报的自然标识 (type, target_id)（模型上正是 ix_reports_target）：
        # 用 target_title 去重会因标题撞车漏种，且标题一改就会重复插入
        existing = (
            (
                await db.execute(
                    select(Report).where(
                        Report.type == data["type"],
                        Report.target_id == data["target_id"],
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            continue
        db.add(Report(**data))
        count += 1
    await db.commit()
    return count


async def main() -> None:
    db = await new_session()
    try:
        count = await seed_reports(db)
        print(f"seeded {count} reports")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
