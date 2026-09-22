"""幂等 seed：基础板块（含父/子层级，供论坛板块广场嵌套展示）。用法 python -m app.modules.content.boards.seed。"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import new_session
from app.modules.content.models import Board
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）

# 父分类 → 子板块。parent_id 为空者为一级大类，子板块挂到对应父板块 slug。
_BOARDS_SPEC = {
    "basic-science": {
        "title": "基础学科",
        "description": "数学、物理、化学、生物等基础学科",
        "children": ["math", "physics", "chemistry", "biology", "cosmos-astronomy"],
    },
    "applied-science": {
        "title": "应用学科",
        "description": "计算机、工程、医学等应用学科",
        "children": ["computer", "engineering", "medicine"],
    },
    "language": {
        "title": "语言学习",
        "description": "英语、俄语、日语等多语言学习",
        "children": ["lang-en", "lang-ja", "lang-ru"],
    },
    "hobby": {
        "title": "兴趣爱好",
        "description": "棋类、音乐、游戏等兴趣板块",
        "children": ["hobby-chess", "hobby-music", "hobby-game"],
    },
    "platform": {
        "title": "平台讨论",
        "description": "社区规则、意见与帮助",
        "children": [],
    },
    "official": {
        "title": "官方发布",
        "description": "官方文章、公告与新闻",
        "children": ["official-news", "official-announcement"],
    },
}

_CHILD_TITLES = {
    "math": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
    "cosmos-astronomy": "宇宙天文",
    "computer": "计算机",
    "engineering": "工程",
    "medicine": "医学",
    "lang-en": "英语",
    "lang-ja": "日语",
    "lang-ru": "俄语",
    "hobby-chess": "棋艺",
    "hobby-music": "音乐",
    "hobby-game": "游戏",
    "official-news": "新闻",
    "official-announcement": "公告",
}


async def seed_boards(db: AsyncSession) -> int:
    created = 0
    for parent_slug, spec in _BOARDS_SPEC.items():
        parent = await db.scalar(select(Board).where(Board.slug == parent_slug))
        if parent is None:
            parent = Board(
                slug=parent_slug,
                title=spec["title"],
                description=spec["description"],
            )
            db.add(parent)
            await db.flush()
            created += 1
        for child_slug in spec["children"]:
            exists = await db.scalar(select(Board).where(Board.slug == child_slug))
            if exists is not None:
                # 已存在但未挂在预期父板块下 → 补挂：否则「重跑 seed 收敛到声明层级」不成立，
                # 历史数据会一直保留错的父子关系
                if exists.parent_id != parent.id:
                    exists.parent_id = parent.id
                    await db.flush()
                continue
            db.add(
                Board(
                    slug=child_slug,
                    # 不做 slug 兜底：漏配 _CHILD_TITLES 时直接 KeyError 炸在 seed 期，
                    # 别把英文 slug 当板块标题静默落库（用户可见）
                    title=_CHILD_TITLES[child_slug],
                    description="",
                    parent_id=parent.id,
                )
            )
            await db.flush()
            created += 1
    return created


async def main() -> None:
    db = await new_session()
    try:
        created = await seed_boards(db)
        await db.commit()
        print(f"seeded {created} boards")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
