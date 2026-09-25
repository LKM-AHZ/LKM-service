"""专栏示例数据。用法：uv run python -m app.modules.content.columns.seed

为 columns / column_posts 填充社区「专栏」页展示所需的示例数据（幂等）。
复刻前端原 mock-columns 的结构，字段以中文原文落库（本期仅中文站场景）。
"""

import asyncio
import json
import uuid
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import now_iso
from app.db.session import new_worker_session as new_session
from app.modules.content.column_models import ColumnPostStatus, ColumnStatus
from app.modules.content.models import (
    Board,
    Column,
    ColumnApplication,
    ColumnPost,
)
from auth import register_models
from auth.entities import Profile, User

register_models()  # 注册 auth ORM 映射类（幂等）

# 种子专栏归属的演示作者用户名（避免依赖具体本地用户）
_SEED_AUTHOR_USERNAME = "column_seed_author"


async def _ensure_user(db: AsyncSession) -> User:
    user = (
        (await db.execute(select(User).where(User.username == _SEED_AUTHOR_USERNAME)))
        .scalars()
        .first()
    )
    if user is None:
        user = User(
            username=_SEED_AUTHOR_USERNAME,
            email=f"{_SEED_AUTHOR_USERNAME}@example.com",
            hashed_password="!seed-only-no-login",  # 不可登录，仅满足 FK
            account_level="local",
        )
        db.add(user)
        await db.flush()
    # 用显式查询而非 user.profile（async 下避免懒加载 MissingGreenlet）
    profile_exists = (
        (await db.execute(select(Profile).where(Profile.user_id == user.id)))
        .scalars()
        .first()
    )
    if profile_exists is None:
        db.add(Profile(user_id=user.id, nickname="理科迷专栏编辑"))
    return user


class _ColumnSeedData(TypedDict):
    slug: str
    title: str
    description: str
    author_name: str
    author_title: str
    author_bio: str
    is_verified: bool
    follower_count: int
    like_count: int
    subscribe_count: int
    article_count: int
    tags: list[str]
    badges: list[str]
    board_slug: str | None  # 关联板块 slug，seed 时解析为 board_id；None 表示无板块


SEED_COLUMNS: list[_ColumnSeedData] = [
    {
        "slug": "cosmic-notes",
        "title": "引力笔记",
        "description": "从黑洞到引力波，用最硬核的物理讲最浪漫的天体。",
        "author_name": "七月·天文台",
        "author_title": "天体物理专栏",
        "author_bio": "一群热爱天体物理的撰稿人，把宇宙的浪漫讲给你听。",
        "is_verified": True,
        "follower_count": 1200,
        "like_count": 3200,
        "subscribe_count": 1500,
        # 展示用占位值：与下方 seed 的 ColumnPost 行数（每栏 3 条）不一致，
        # 详情页只列真实行。真实统计应由后端聚合，勿以此判断数据是否损坏
        "article_count": 15,
        "tags": ["引力波", "黑洞", "天体物理"],
        "badges": ["机构认证", "签约作者"],
        "board_slug": "physics",
    },
    {
        "slug": "edu-lab",
        "title": "科学教育实验室",
        "description": "把复杂的科学概念拆解成可上手的课堂教学设计。",
        "author_name": "七月·教育",
        "author_title": "科学教育专栏",
        "author_bio": "专注科学教育的课程设计与教学方法研究。",
        "is_verified": True,
        "follower_count": 890,
        "like_count": 1800,
        "subscribe_count": 950,
        "article_count": 8,
        "tags": ["科学教育", "课程设计", "科普"],
        "badges": ["机构认证"],
        "board_slug": None,
    },
    {
        "slug": "academic-writing",
        "title": "学术写作指南",
        "description": "论文写作、投稿经验与科研方法论的实战分享。",
        "author_name": "科研方法论编辑部",
        "author_title": "学术写作专栏",
        "author_bio": "分享论文写作、投稿与科研方法的实战经验。",
        "is_verified": True,
        "follower_count": 650,
        "like_count": 2100,
        "subscribe_count": 720,
        "article_count": 12,
        "tags": ["学术写作", "论文", "科研方法"],
        "badges": ["签约作者"],
        "board_slug": None,
    },
    {
        "slug": "algorithm-beauty",
        "title": "算法之美",
        "description": "算法与数据结构，从入门到优雅的 Python 实现。",
        "author_name": "算法工坊",
        "author_title": "算法专栏",
        "author_bio": "把算法与数据结构讲得明白、写得优雅。",
        "is_verified": False,
        "follower_count": 450,
        "like_count": 1500,
        "subscribe_count": 480,
        "article_count": 20,
        "tags": ["算法", "数据结构", "Python"],
        "badges": [],
        "board_slug": "computer",
    },
]

# 每专栏 3 篇示例文章（title / summary / content 为纯文本或简单 Markdown）
_SEED_POSTS_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "cosmic-notes": [
        {
            "title": "引力波的前世今生",
            "summary": "从广义相对论的预言到 LIGO 的世纪探测。",
            "content": "# 引力波的前世今生\n\n1916 年爱因斯坦预言了引力波……",
        },
        {
            "title": "黑洞照片是怎么拍的",
            "summary": "事件视界望远镜如何给黑洞「拍照」。",
            "content": "# 黑洞照片是怎么拍的\n\nEHT 项目用全球望远镜组成阵列……",
        },
        {
            "title": "宇宙膨胀的速度之谜",
            "summary": "哈勃常数之争与暗能量的角色。",
            "content": "# 宇宙膨胀的速度之谜\n\n暗能量让宇宙加速膨胀……",
        },
    ],
    "edu-lab": [
        {
            "title": "如何设计一节好玩的物理课",
            "summary": "把生活现象带进课堂的 5 个方法。",
            "content": "# 如何设计一节好玩的物理课\n\n从现象出发……",
        },
        {
            "title": "用实验激发孩子的科学兴趣",
            "summary": "低成本实验清单与课堂组织技巧。",
            "content": "# 用实验激发孩子的科学兴趣\n\n实验是最好的老师……",
        },
        {
            "title": "STEM 教育的未来",
            "summary": "跨学科融合与项目式学习。",
            "content": "# STEM 教育的未来\n\n跨学科是趋势……",
        },
    ],
    "academic-writing": [
        {
            "title": "论文标题怎么取",
            "summary": "让标题兼具信息量与吸引力的原则。",
            "content": "# 论文标题怎么取\n\n好的标题是成功的一半……",
        },
        {
            "title": "审稿人最在意什么",
            "summary": "从审稿视角反推写作要点。",
            "content": "# 审稿人最在意什么\n\n清晰、可复现、有新意……",
        },
        {
            "title": "从初稿到投稿：修订清单",
            "summary": "投稿前逐项自查清单。",
            "content": "# 从初稿到投稿：修订清单\n\n结构、图表、引用……",
        },
    ],
    "algorithm-beauty": [
        {
            "title": "从斐波那契看动态规划",
            "summary": "递推、备忘录与状态转移。",
            "content": "# 从斐波那契看动态规划\n\n先写朴素递归……",
        },
        {
            "title": "二分查找的边界陷阱",
            "summary": "左闭右开与循环不变量。",
            "content": "# 二分查找的边界陷阱\n\n区间定义不清必出错……",
        },
        {
            "title": "Python 中的单调栈应用",
            "summary": "解决「下一个更大元素」类问题。",
            "content": "# Python 中的单调栈应用\n\n维护一个递增栈……",
        },
    ],
}


async def _board_id(db: AsyncSession, slug: str | None) -> uuid.UUID | None:
    """按 slug 解析 Board 主键；对应板块未 seed 时返回 None（不报错）。"""
    if not slug:
        return None
    return await db.scalar(select(Board.id).where(Board.slug == slug))


async def seed_columns(db: AsyncSession) -> int:
    count = 0
    user = await _ensure_user(db)
    for data in SEED_COLUMNS:
        slug = data["slug"]
        existing = (
            (await db.execute(select(Column).where(Column.slug == slug)))
            .scalars()
            .first()
        )
        if existing is not None:
            continue
        board_id = await _board_id(db, data.get("board_slug"))
        # 先建申请记录并 flush，才能把 column.application_id 回填——真实审核流
        # （service._ensure_column_for_application）就是这么做的。原先申请行建了但没回链，
        # 结果 applicationId 对每个种子专栏都是 null、申请行成了孤儿。
        application = ColumnApplication(
            user_id=user.id,
            title=str(data["title"]),
            description=str(data["description"]),
            reason="示例专栏申请",
            status="approved",
            reviewed_at=now_iso(),
        )
        db.add(application)
        await db.flush()
        col = Column(
            owner_id=user.id,
            title=data["title"],
            description=data["description"],
            slug=slug,
            author_name=data["author_name"],
            author_title=data["author_title"],
            author_bio=data["author_bio"],
            is_verified=data["is_verified"],
            follower_count=data["follower_count"],
            like_count=data["like_count"],
            subscribe_count=data["subscribe_count"],
            article_count=data["article_count"],
            tags=json.dumps(data["tags"], ensure_ascii=False),
            badges=json.dumps(data["badges"], ensure_ascii=False),
            board_id=board_id,
            application_id=application.id,
            status=ColumnStatus.ACTIVE,
        )
        db.add(col)
        await db.flush()
        for p in _SEED_POSTS_TEMPLATES.get(slug, []):
            db.add(
                ColumnPost(
                    column_id=col.id,
                    author_id=user.id,
                    title=p["title"],
                    summary=p["summary"],
                    content=p["content"],
                    status=ColumnPostStatus.PUBLISHED,
                    published_at=now_iso(),
                    view_count=0,
                    like_count=0,
                    comment_count=0,
                )
            )
        count += 1
    await db.commit()
    return count


async def main() -> None:
    db = await new_session()
    try:
        count = await seed_columns(db)
        print(f"seeded {count} columns")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
