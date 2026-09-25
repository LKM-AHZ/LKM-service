"""贡献/积分系统种子数据。用法：``python -m app.modules.points.seed``

幂等 seed 12 成就 / 5 任务 / 6 兑换（按唯一 key 查存在则跳过）。
i18n 文案统一用前端已定义的原始 key 字符串（name_key/desc_key），不做翻译。
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import new_worker_session as new_session
from app.modules.points.models import Achievement, ExchangeItem, Task
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）

# ------------------------------- 成就 12 -----------------------------------
_ACHIEVEMENTS: list[dict] = [
    dict(
        key="a1",
        name_key="contributionData.achievements.a1.name",
        desc_key="contributionData.achievements.a1.description",
        category="special",
        type="onboarding",
        threshold=1,
        icon="tabler:star",
        sort_order=1,
    ),
    dict(
        key="a2",
        name_key="contributionData.achievements.a2.name",
        desc_key="contributionData.achievements.a2.description",
        category="posting",
        type="post_count",
        threshold=1,
        icon="tabler:pencil",
        sort_order=2,
    ),
    dict(
        key="a3",
        name_key="contributionData.achievements.a3.name",
        desc_key="contributionData.achievements.a3.description",
        category="posting",
        type="post_count",
        threshold=10,
        icon="tabler:pencil-plus",
        sort_order=3,
    ),
    dict(
        key="a4",
        name_key="contributionData.achievements.a4.name",
        desc_key="contributionData.achievements.a4.description",
        category="posting",
        type="featured_count",
        threshold=1,
        icon="tabler:star-filled",
        sort_order=4,
    ),
    dict(
        key="a5",
        name_key="contributionData.achievements.a5.name",
        desc_key="contributionData.achievements.a5.description",
        category="posting",
        type="post_count",
        threshold=100,
        icon="tabler:writing",
        sort_order=5,
    ),
    dict(
        key="a6",
        name_key="contributionData.achievements.a6.name",
        desc_key="contributionData.achievements.a6.description",
        category="helping",
        type="accepted_answers",
        threshold=5,
        icon="tabler:heart-handshake",
        sort_order=6,
    ),
    dict(
        key="a7",
        name_key="contributionData.achievements.a7.name",
        desc_key="contributionData.achievements.a7.description",
        category="helping",
        type="accepted_answers",
        threshold=20,
        icon="tabler:brain",
        sort_order=7,
    ),
    dict(
        key="a8",
        name_key="contributionData.achievements.a8.name",
        desc_key="contributionData.achievements.a8.description",
        category="files",
        type="approved_files",
        threshold=10,
        icon="tabler:file-check",
        sort_order=8,
    ),
    dict(
        key="a9",
        name_key="contributionData.achievements.a9.name",
        desc_key="contributionData.achievements.a9.description",
        category="activity",
        type="checkin_streak",
        threshold=7,
        icon="tabler:calendar-check",
        sort_order=9,
    ),
    dict(
        key="a10",
        name_key="contributionData.achievements.a10.name",
        desc_key="contributionData.achievements.a10.description",
        category="activity",
        type="checkin_streak",
        threshold=30,
        icon="tabler:calendar-star",
        sort_order=10,
    ),
    dict(
        key="a11",
        name_key="contributionData.achievements.a11.name",
        desc_key="contributionData.achievements.a11.description",
        category="activity",
        type="project_count",
        threshold=3,
        icon="tabler:rocket",
        sort_order=11,
    ),
    dict(
        key="a12",
        name_key="contributionData.achievements.a12.name",
        desc_key="contributionData.achievements.a12.description",
        category="special",
        type="column_articles",
        threshold=5,
        icon="tabler:article",
        sort_order=12,
    ),
]


# ------------------------------- 任务 5 ------------------------------------
_TASKS: list[dict] = [
    dict(
        key="t1",
        title_key="contributionData.tasks.t1.title",
        desc_key="contributionData.tasks.t1.description",
        category="checkin",
        requirement_count=1,
        reward_points=5,
        sort_order=1,
    ),
    dict(
        key="t2",
        title_key="contributionData.tasks.t2.title",
        desc_key="contributionData.tasks.t2.description",
        category="post",
        requirement_count=1,
        reward_points=10,
        sort_order=2,
    ),
    dict(
        key="t3",
        title_key="contributionData.tasks.t3.title",
        desc_key="contributionData.tasks.t3.description",
        category="answer",
        requirement_count=3,
        reward_points=30,
        sort_order=3,
    ),
    dict(
        key="t4",
        title_key="contributionData.tasks.t4.title",
        desc_key="contributionData.tasks.t4.description",
        category="like",
        requirement_count=10,
        reward_points=5,
        sort_order=4,
    ),
    dict(
        key="t5",
        title_key="contributionData.tasks.t5.title",
        desc_key="contributionData.tasks.t5.description",
        category="file_upload",
        requirement_count=1,
        reward_points=15,
        sort_order=5,
    ),
]


# ------------------------------- 兑换 6 ------------------------------------
_EXCHANGE_ITEMS: list[dict] = [
    dict(
        key="e1",
        name_key="contributionData.exchangeItems.e1.name",
        desc_key="contributionData.exchangeItems.e1.description",
        points_cost=200,
        stock=-1,
        is_virtual=True,
        sort_order=1,
    ),
    dict(
        key="e2",
        name_key="contributionData.exchangeItems.e2.name",
        desc_key="contributionData.exchangeItems.e2.description",
        points_cost=500,
        stock=-1,
        is_virtual=True,
        sort_order=2,
    ),
    dict(
        key="e3",
        name_key="contributionData.exchangeItems.e3.name",
        desc_key="contributionData.exchangeItems.e3.description",
        points_cost=1000,
        stock=5,
        is_virtual=True,
        sort_order=3,
    ),
    dict(
        key="e4",
        name_key="contributionData.exchangeItems.e4.name",
        desc_key="contributionData.exchangeItems.e4.description",
        points_cost=800,
        stock=50,
        is_virtual=False,
        sort_order=4,
    ),
    dict(
        key="e5",
        name_key="contributionData.exchangeItems.e5.name",
        desc_key="contributionData.exchangeItems.e5.description",
        points_cost=500,
        stock=100,
        is_virtual=False,
        sort_order=5,
    ),
    dict(
        key="e6",
        name_key="contributionData.exchangeItems.e6.name",
        desc_key="contributionData.exchangeItems.e6.description",
        points_cost=1500,
        stock=30,
        is_virtual=False,
        sort_order=6,
    ),
]


async def seed_achievements(db: AsyncSession) -> int:
    """幂等插入 12 条成就，返回本次插入条数。"""
    count = 0
    for data in _ACHIEVEMENTS:
        key = data["key"]
        existing = await db.scalar(
            select(Achievement.key).where(Achievement.key == key)
        )
        if existing is not None:
            continue
        db.add(Achievement(**data))
        count += 1
    return count


async def seed_tasks(db: AsyncSession) -> int:
    """幂等插入 5 条任务，返回本次插入条数。"""
    count = 0
    for data in _TASKS:
        key = data["key"]
        existing = await db.scalar(select(Task.key).where(Task.key == key))
        if existing is not None:
            continue
        db.add(Task(**data))
        count += 1
    return count


async def seed_exchange_items(db: AsyncSession) -> int:
    """幂等插入 6 条兑换项，返回本次插入条数。"""
    count = 0
    for data in _EXCHANGE_ITEMS:
        key = data["key"]
        existing = await db.scalar(
            select(ExchangeItem.key).where(ExchangeItem.key == key)
        )
        if existing is not None:
            continue
        db.add(ExchangeItem(**data))
        count += 1
    return count


async def main() -> None:
    db = await new_session()
    try:
        ach = await seed_achievements(db)
        tsk = await seed_tasks(db)
        exc = await seed_exchange_items(db)
        await db.commit()
        print(f"achievements: {ach} inserted, tasks: {tsk}, exchange_items: {exc}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
