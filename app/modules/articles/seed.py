"""官方文章示例数据。用法：uv run python -m app.modules.articles.seed"""

import asyncio
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import now_iso
from app.db.session import new_worker_session as new_session
from app.modules.articles.models import Article, ArticleCategory
from auth import register_models

register_models()  # 注册 auth ORM 映射类（幂等）

# 文章分类种子：slug 幂等；engineering 是 blog produce 默认分类，必须存在
_CATEGORIES: list[dict[str, int | str]] = [
    {"slug": "announcement", "title": "官方公告", "sort": 0},
    {"slug": "news", "title": "新闻", "sort": 1},
    {"slug": "science", "title": "科学", "sort": 2},
    {"slug": "engineering", "title": "工程", "sort": 3},
]

class _LazyMarkdown:
    """惰性正文占位：seed 时才读盘。

    模块 import（连测试收集也算）不该有文件系统副作用——原先在模块级 read_text，
    文件被移动/删除会让任何 import 本模块的地方直接 FileNotFoundError。
    """

    def render(self) -> str:
        return (Path(__file__).parent / "markdown-test.md").read_text(encoding="utf-8")


# Markdown 渲染测试文章（独立 md 文件作为单一内容源）
_MARKDOWN_TEST_CONTENT = _LazyMarkdown()

# 文章种子：category 存分类 slug，创建时解析为 category_id
SEED_ARTICLES: list[dict[str, object]] = [
    {
        "slug": "welcome-to-lkm",
        "title": "欢迎来到理科迷",
        "description": "理科迷官方社区正式上线，欢迎各位理科爱好者。",
        "cover": None,
        "category": "announcement",
        "content": "# 欢迎来到理科迷\n\n理科迷是一个理科爱好者的交流社区。\n\n- 分享知识\n- 参与竞赛\n- 探索科学",
        "publisher": "理科迷运营组",
        "department": "官方",
        "keywords": "公告,上线",
    },
    {
        "slug": "ai-in-science-education",
        "title": "AI 在理科教育中的应用",
        "description": "浅谈人工智能如何改变理科学习方式。",
        "cover": None,
        "category": "news",
        "content": "# AI 在理科教育中的应用\n\n人工智能正在重塑教育。\n\n## 自适应学习\n\nAI 可以根据学生水平推荐题目。",
        "publisher": "理科迷编辑部",
        "department": "内容",
        "keywords": "AI,教育",
    },
    {
        "slug": "why-the-sky-is-blue",
        "title": "为什么天空是蓝色的",
        "description": "用瑞利散射解释我们熟悉的自然现象。",
        "cover": None,
        "category": "science",
        "content": "# 为什么天空是蓝色的\n\n太阳光进入大气层后发生**瑞利散射**。\n\n```text\n散射强度 ∝ 1/波长^4\n```",
        "publisher": "理科迷编辑部",
        "department": "内容",
        "keywords": "科普,光学",
    },
    {
        "slug": "engineering-practices",
        "title": "工程实践：从零搭建一个服务",
        "description": "一次最小可用的后端服务搭建记录。",
        "cover": None,
        "category": "engineering",
        "content": "# 工程实践\n\n从零搭建一个服务，需要关注：\n\n1. 分层\n2. 测试\n3. 部署",
        "publisher": "理科迷工程组",
        "department": "工程",
        "keywords": "工程,后端",
    },
    {
        "slug": "markdown-test",
        "title": "Markdown 渲染测试",
        "description": "验证文章详情页 Markdown 渲染与侧栏目录（TOC）效果。",
        "cover": None,
        "category": "news",
        "content": _MARKDOWN_TEST_CONTENT,
        "publisher": "理科迷编辑部",
        "department": "测试",
        "keywords": "测试,Markdown",
    },
]


async def seed_categories(db: AsyncSession) -> int:
    """幂等写入文章分类：按 slug 去重，存在则跳过，返回实际新建条数。

    自己 commit：只 flush 的话单独调用（如运维只补分类）在会话关闭时全部丢失，
    且 main() 里会跟 seed_articles 的失败绑死——articles 报错就把分类一起回滚。
    """
    created = 0
    for spec in _CATEGORIES:
        exists = await db.scalar(
            select(ArticleCategory.id).where(ArticleCategory.slug == spec["slug"])
        )
        if exists is not None:
            continue
        db.add(ArticleCategory(**spec))
        await db.flush()
        created += 1
    await db.commit()
    return created


async def _resolve_category_id(db: AsyncSession, slug: str) -> uuid.UUID:
    """按分类 slug 解析 category_id；不存在则抛 KeyError（调用方保证分类已 seed）。"""
    category_id = await db.scalar(
        select(ArticleCategory.id).where(ArticleCategory.slug == slug)
    )
    if category_id is None:
        raise KeyError(f"article category slug not found: {slug}")
    return category_id


async def seed_articles(db: AsyncSession) -> int:
    """幂等写入官方文章：按 slug 去重；category slug 解析为 category_id，状态 published。"""
    count = 0
    for data in SEED_ARTICLES:
        slug = data["slug"]
        existing = (
            await db.execute(select(Article).where(Article.slug == slug))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        category_slug = data["category"]
        # 不能用 assert：python -O 会把它整条剥离，校验静默消失
        if not isinstance(category_slug, str):
            raise TypeError(f"article category must be a slug string: {category_slug!r}")
        fields = {k: v for k, v in data.items() if k != "category"}
        content = fields.get("content")
        if isinstance(content, _LazyMarkdown):  # 惰性正文：此时才读盘
            fields["content"] = content.render()
        article = Article(
            published=now_iso(),
            status="published",
            category_id=await _resolve_category_id(db, category_slug),
            **fields,
        )
        db.add(article)
        count += 1
    await db.commit()
    return count


async def main() -> None:
    db = await new_session()
    try:
        category_count = await seed_categories(db)
        print(f"seeded {category_count} categories")
        count = await seed_articles(db)
        print(f"seeded {count} articles")
    except Exception:
        # 显式回滚：失败时让事务状态确定，也让运维看到明确的失败原因（而不是只有关闭时的隐式回滚）
        await db.rollback()
        raise
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
