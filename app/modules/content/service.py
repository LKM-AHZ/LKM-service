import datetime as _dt
import json
import re
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    TTL_ITEM_S,
    TTL_LIST_S,
    bump_collection_version,
    cached_read,
    collection_version,
    make_key,
)
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError
from app.core.metrics import post_created_total
from app.db.base import now_iso
from app.db.repo import get_or_raise
from app.modules.auth.snapshot import get_user_snapshot, get_user_snapshot_batch
from app.modules.content.boards.errors import BoardErr
from app.modules.content.boards.schemas import (
    BanRequest,
    BoardApplicationCreate,
    BoardApplicationOut,
    BoardCreate,
    BoardOut,
    BoardUpdate,
    ReviewBoardApplicationRequest,
)
from app.modules.content.column_models import (
    COLUMN_TABLE_PLAN,
    ColumnApplicationStatus,
    ColumnPostStatus,
)
from app.modules.content.columns.errors import ColumnErr
from app.modules.content.columns.schemas import (
    ColumnApplicationCreate,
    ColumnApplicationInfo,
    ColumnApplicationReview,
    ColumnInfo,
    ColumnPostCreate,
    ColumnPostInfo,
)
from app.modules.content.counters import bump_content_counter, read_count
from app.modules.content.errors import ContentErr
from app.modules.content.models import (
    Board,
    BoardApplication,
    BoardBan,
    Column,
    ColumnApplication,
    ColumnPost,
    ContentComment,
    ContentItem,
    ContentLike,
    ContentStatus,
    ContentType,
    QAAnswer,
    QAQuestion,
    QAQuestionImage,
)
from app.modules.content.qa.errors import QaErr
from app.modules.content.qa.schemas import (
    AnswerCreate,
    AnswerOut,
    QuestionCreate,
    QuestionDetail,
    QuestionOut,
)
from app.modules.content.schemas import (
    ContentCommentCreate,
    ContentCommentInfo,
    ContentItemCreate,
    ContentItemInfo,
)
from app.modules.exam.models import Exam, ExamCertificate
from app.modules.points.rules import enqueue_points_event
from app.modules.points.service import reward, spend

READING_WPM = 300  # 每 300 字约 1 分钟阅读时间


def _excerpt_of(content: str, limit: int = 150) -> str:
    text = re.sub(r"<[^>]+>", " ", content)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _reading_time(content: str) -> int:
    if not content:
        return 0
    return max(1, len(content) // READING_WPM)


def _item_to_schema(
    item: ContentItem,
    author_name: str,
    column_title: str | None = None,
) -> ContentItemInfo:
    return ContentItemInfo.model_validate(item).model_copy(
        update={
            "author_name": author_name,
            "column_title": column_title or "",
            "reading_time": _reading_time(item.content),
        }
    )


def _comment_to_schema(c: ContentComment, author_name: str) -> ContentCommentInfo:
    return ContentCommentInfo.model_validate(c).model_copy(
        update={"author_name": author_name}
    )


async def _author_map(db: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    ids = {i for i in user_ids if i}
    if not ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(ids))
    return {uid: s.display_name for uid, s in snaps.items()}


async def _column_title_map(db: AsyncSession, column_ids: list[int]) -> dict[int, str]:
    if not column_ids:
        return {}
    result = await db.execute(select(Column).where(Column.id.in_(set(column_ids))))
    columns = result.scalars().all()
    return {c.id: c.title for c in columns}


async def list_items(
    db: AsyncSession,
    page: int = 1,
    limit: int = 20,
    board_id: int | None = None,
    content_type: str | None = None,
) -> PageData[ContentItemInfo]:
    base = select(ContentItem).where(ContentItem.status == ContentStatus.PUBLISHED)
    if board_id:
        base = base.where(ContentItem.board_id == board_id)
    if content_type:
        base = base.where(ContentItem.content_type == content_type)

    # 计数走短 TTL 缓存 + content 集合版本号：任何增删 ContentItem 的行都会
    # bump_content_version() 递增集合版本，使旧(count)键立即失效，杜绝脏计数。
    async def _count() -> int:
        count_stmt = select(func.count()).select_from(base.subquery())
        total: int | None = await db.scalar(count_stmt)
        return total or 0

    ver = await collection_version("content")
    total_count = await cached_read(
        make_key("content:list:total", ver, board_id or "", content_type or ""),
        60,
        _count,
    )

    stmt = (
        base.order_by(ContentItem.is_pinned.desc(), ContentItem.id.desc())
        .offset(paginate_offset(page, limit))
        .limit(limit)
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    author_ids = [i.author_id for i in items if i.author_id]
    column_ids = [i.column_id for i in items if i.column_id]
    names = await _author_map(db, author_ids)
    cols = await _column_title_map(db, column_ids)
    out = [
        _item_to_schema(
            i,
            names.get(i.author_id or -1, "") if i.author_id else (i.publisher or ""),
            cols.get(i.column_id or -1) if i.column_id else None,
        )
        for i in items
    ]
    return PageData(
        items=out,
        total=total_count,
        page=page,
        pages=paginate_pages(total_count, limit),
    )


# 视图计数写会话缝：GraphQL 用只读会话不能写，故 bump 自建独立写会话。
# 默认 new_session()；测试可替换为 conftest 内存会话以断言落库（仿 blog git _session_factory）。
async def _new_write_session() -> AsyncSession:
    from app.db.session import new_session

    return await new_session()


async def bump_item_view(item_id: int) -> None:
    """原子地给内容项 view_count +1（供公开详情阅读计数增长路径）。

    前端详情走 GraphQL ``contentItem``，此前 get_item 恒 bump_view=False 且 REST 无详情
    端点，导致 view_count 无增长路径。GraphQL 用只读会话（读后显式 rollback），不能在其上写，
    故自建独立写会话原子 UPDATE 并 commit，原子 UPDATE 避免并发 read-modify-write 丢计数。
    """
    dbw = await _new_write_session()
    try:
        await dbw.execute(
            sa_update(ContentItem)
            .where(ContentItem.id == item_id)
            .values(view_count=ContentItem.view_count + 1)
        )
        await dbw.commit()
    finally:
        await dbw.close()


async def get_item(
    db: AsyncSession, item_id: int, bump_view: bool = False
) -> ContentItemInfo:
    item = await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    if bump_view:
        item.view_count += 1
        await db.flush()

    author_name = ""
    if item.author_id:
        names = await _author_map(db, [item.author_id])
        author_name = names.get(item.author_id, "")
    else:
        author_name = item.publisher or ""
    column_title = ""
    if item.column_id:
        cols = await _column_title_map(db, [item.column_id])
        column_title = cols.get(item.column_id, "")
    return _item_to_schema(item, author_name, column_title or None)


async def get_item_by_slug(db: AsyncSession, slug: str) -> ContentItemInfo:
    item = await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.slug == slug
    )
    author_name = ""
    if item.author_id:
        names = await _author_map(db, [item.author_id])
        author_name = names.get(item.author_id, "")
    else:
        author_name = item.publisher or ""
    column_title = ""
    if item.column_id:
        cols = await _column_title_map(db, [item.column_id])
        column_title = cols.get(item.column_id, "")
    return _item_to_schema(item, author_name, column_title or None)


async def _require_unique_slug(db: AsyncSession, slug: str | None) -> None:
    if not slug:
        return
    # 应用层唯一校验，返回领域错误码 SLUG_TAKEN（DB 侧唯一索引兜底）
    existing = (
        (
            await db.execute(
                select(ContentItem.id).where(ContentItem.slug == slug).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise BizError(ContentErr.SLUG_TAKEN)


async def create_item(
    db: AsyncSession,
    author_id: int,
    info: ContentItemCreate,
) -> ContentItemInfo:
    # 发言准入：板块存在 / 认证 / 日限发（仅社区用户写作体裁）
    if info.content_type in (
        ContentType.DISCUSSION,
        ContentType.COLUMN_POST,
        ContentType.BLOG_POST,
        ContentType.QA,
    ):
        from app.modules.content.boards.service import check_post_allowed

        await check_post_allowed(db, info.board_id, author_id)

    await get_or_raise(db, Board, ContentErr.BOARD_NOT_FOUND, Board.id == info.board_id)

    if info.content_type == ContentType.COLUMN_POST:
        if not info.column_id:
            raise BizError(ContentErr.COLUMN_NOT_FOUND)
        await get_or_raise(
            db, Column, ContentErr.COLUMN_NOT_FOUND, Column.id == info.column_id
        )

    if info.content_type == ContentType.ARTICLE:
        await _require_unique_slug(db, info.slug)

    status = info.status
    if info.content_type == ContentType.DISCUSSION:
        status = ContentStatus.PUBLISHED  # 讨论帖无审稿，发即公开

    item = ContentItem(
        content_type=info.content_type,
        board_id=info.board_id,
        author_id=author_id,
        publisher=info.publisher,
        department=info.department,
        column_id=(
            info.column_id if info.content_type == ContentType.COLUMN_POST else None
        ),
        qa_question_id=(
            info.qa_question_id if info.content_type == ContentType.QA else None
        ),
        slug=info.slug if info.content_type == ContentType.ARTICLE else None,
        title=info.title,
        excerpt=_excerpt_of(info.content),
        content=info.content,
        summary=info.summary,
        cover=info.cover,
        keywords=json.dumps(info.keywords, ensure_ascii=False)
        if info.keywords
        else None,
        tags=json.dumps(info.tags, ensure_ascii=False),
        status=status,
        is_pinned=info.is_pinned,
        is_featured=info.is_featured,
        published_at=None,
    )
    db.add(item)
    await db.flush()

    # 积分事件（异步计分，不阻塞 200）
    await enqueue_points_event(db, author_id, "post", f"item:{item.id}")
    await bump_collection_version("content")

    # M0.5.2 业务指标：本方法统一写 content_items（discussion/column_post/
    # article/blog_post/qa），谓成功落库即全内容 dumps 的“发帖/新版产出”。
    post_created_total.labels(item.content_type).inc()

    names = await _author_map(db, [author_id])
    return _item_to_schema(item, names.get(author_id, ""))


async def delete_item(
    db: AsyncSession,
    item_id: int,
    current_user_id: int,
    as_admin: bool = False,
) -> int:
    """删除统一内容项并返回作者 id（admin 代删时可能无作者返回 0）。"""
    item = await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    author_id = item.author_id or -1
    await db.delete(item)
    await db.flush()
    await bump_collection_version("content")
    return author_id


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


async def like_item(db: AsyncSession, item_id: int, user_id: int) -> int:
    await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    existing = (
        (
            await db.execute(
                select(ContentLike).where(
                    ContentLike.content_id == item_id,
                    ContentLike.user_id == user_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return await read_count(db, item_id, "like_count")

    db.add(ContentLike(content_id=item_id, user_id=user_id))
    await db.flush()
    await enqueue_points_event(db, user_id, "like", f"item:{item_id}")
    # M6.10：计数走 Redis 增量链路（明细行已落库，是真相源）；Redis 不可用则直改 DB
    return await bump_content_counter(db, item_id, "like_count", 1)


async def unlike_item(db: AsyncSession, item_id: int, user_id: int) -> int:
    await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    existing = (
        (
            await db.execute(
                select(ContentLike).where(
                    ContentLike.content_id == item_id,
                    ContentLike.user_id == user_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        return await read_count(db, item_id, "like_count")
    await db.delete(existing)
    await db.flush()
    return await bump_content_counter(db, item_id, "like_count", -1)


async def list_comments(
    db: AsyncSession,
    item_id: int,
    page: int = 1,
    limit: int = 20,
) -> PageData[ContentCommentInfo]:
    await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    total = (
        await db.scalar(
            select(func.count(ContentComment.id)).where(
                ContentComment.content_id == item_id
            )
        )
        or 0
    )
    stmt = (
        select(ContentComment)
        .where(ContentComment.content_id == item_id)
        .order_by(ContentComment.floor_number.asc())
        .offset(paginate_offset(page, limit))
        .limit(limit)
    )
    result = await db.execute(stmt)
    comments = result.scalars().all()
    names = await _author_map(db, [c.user_id for c in comments])
    items = [_comment_to_schema(c, names.get(c.user_id, "")) for c in comments]
    return PageData(
        items=items,
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def list_all_comments(db: AsyncSession, item_id: int) -> list[ContentCommentInfo]:
    """一次取回某内容的全部评论（floor 升序），供 GraphQL 组装评论树。"""
    await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    stmt = (
        select(ContentComment)
        .where(ContentComment.content_id == item_id)
        .order_by(ContentComment.floor_number.asc())
    )
    result = await db.execute(stmt)
    comments = result.scalars().all()
    names = await _author_map(db, [c.user_id for c in comments])
    return [_comment_to_schema(c, names.get(c.user_id, "")) for c in comments]


async def create_comment(
    db: AsyncSession,
    item_id: int,
    user_id: int,
    info: ContentCommentCreate,
) -> ContentCommentInfo:
    await get_or_raise(
        db, ContentItem, ContentErr.CONTENT_NOT_FOUND, ContentItem.id == item_id
    )
    if info.parent_id is not None:
        await get_or_raise(
            db,
            ContentComment,
            ContentErr.COMMENT_NOT_FOUND,
            ContentComment.id == info.parent_id,
            ContentComment.content_id == item_id,
        )

    result = await db.execute(
        select(ContentComment)
        .where(ContentComment.content_id == item_id)
        .order_by(ContentComment.floor_number.desc())
        .limit(1)
    )
    floor = result.scalars().first()
    next_floor = floor.floor_number + 1 if floor else 1

    comment = ContentComment(
        content_id=item_id,
        user_id=user_id,
        content=info.content,
        floor_number=next_floor,
        parent_id=info.parent_id,
    )
    db.add(comment)
    await db.flush()
    # M6.10：评论计数同走计数链路（评论明细行已落库，是真相源）
    await bump_content_counter(db, item_id, "comment_count", 1)
    await enqueue_points_event(db, user_id, "comment", f"comment:{comment.id}")

    names = await _author_map(db, [comment.user_id])
    return _comment_to_schema(comment, names.get(comment.user_id, ""))


# ---- 供 blog publish 等内部调用（直接落 content_items）----


async def publish_blog_item(
    db: AsyncSession,
    owner_id: int,
    *,
    board_id: int,
    slug: str | None,
    title: str,
    content: str,
    summary: str | None,
    cover: str | None,
    tags: list[str],
) -> int:
    """把 blog 发布产物落成一条 content_items（content_type=blog_post）。

    幂等：同 slug 更新现有行（重发），否则新建。返回 content_items.id。
    """
    existing = None
    if slug:
        existing = (
            (
                await db.execute(
                    select(ContentItem).where(ContentItem.slug == slug).limit(1)
                )
            )
            .scalars()
            .first()
        )

    if existing is not None:
        existing.title = title
        existing.content = content
        existing.excerpt = _excerpt_of(content)
        existing.summary = summary or existing.summary
        existing.cover = cover or existing.cover
        existing.tags = json.dumps(tags, ensure_ascii=False)
        existing.content_type = ContentType.BLOG_POST
        existing.board_id = board_id
        existing.status = ContentStatus.PUBLISHED
        existing.published_at = existing.published_at or _now()
        await db.flush()
        return existing.id

    item = ContentItem(
        content_type=ContentType.BLOG_POST,
        board_id=board_id,
        author_id=owner_id,
        publisher=None,
        department=None,
        slug=slug or None,
        title=title,
        excerpt=_excerpt_of(content),
        content=content,
        summary=summary,
        cover=cover,
        tags=json.dumps(tags, ensure_ascii=False),
        status=ContentStatus.PUBLISHED,
        published_at=_now(),
    )
    db.add(item)
    await db.flush()
    # M0.5.2：仅“新建”分支记入博客发布产出；同 slug 重发（上面 existing 更新早退）不复计。
    post_created_total.labels(ContentType.BLOG_POST).inc()
    await enqueue_points_event(db, owner_id, "post", f"item:{item.id}")
    await bump_collection_version("content")
    return item.id


# =====================================================================
# columns(专栏) 子域 —— 归入 content 单一 service 命名空间。
# 原实现迁自 app/modules/content/columns/service.py；columns/service.py 现为薄 re-export 桩。
# 表(Column/ColumnApplication/ColumnPost)与 schemas/errors 保持原样，事件语义不变。
# =====================================================================


def get_column_plan() -> dict[str, Any]:
    return {
        "status": "implemented_minimal",
        "tables": COLUMN_TABLE_PLAN,
        "next_steps": [
            "Add authentication before write operations",
            "Restrict review APIs to administrators",
            "Add pagination, search, and board relation",
        ],
    }


async def create_application(
    db: AsyncSession, user_id: int, info: ColumnApplicationCreate
) -> ColumnApplicationInfo:
    app = ColumnApplication(
        user_id=user_id,
        title=info.title,
        description=info.description,
        reason=info.reason,
    )
    db.add(app)
    await db.flush()
    return ColumnApplicationInfo.model_validate(app)


async def list_applications(
    db: AsyncSession, page: int = 1, limit: int | None = None
) -> PageData[ColumnApplicationInfo]:
    """申请列表，统一返回 ``PageData``。不传 ``limit`` 时返回全部（page 恒为 1，pages 视总数）。"""
    total = await db.scalar(select(func.count()).select_from(ColumnApplication)) or 0
    stmt = select(ColumnApplication).order_by(ColumnApplication.id.desc())
    if limit is not None:
        stmt = stmt.offset(paginate_offset(page, limit)).limit(limit)
    apps = (await db.execute(stmt)).scalars().all()
    return PageData(
        items=[ColumnApplicationInfo.model_validate(a) for a in apps],
        total=total,
        page=page,
        pages=paginate_pages(total, limit) if limit else (1 if total else 0),
    )


async def get_application(
    db: AsyncSession, application_id: int
) -> ColumnApplicationInfo:
    return ColumnApplicationInfo.model_validate(
        await get_or_raise(
            db,
            ColumnApplication,
            ColumnErr.APPLICATION_NOT_FOUND,
            ColumnApplication.id == application_id,
        )
    )


async def review_application(
    db: AsyncSession,
    application_id: int,
    info: ColumnApplicationReview,
    reviewer_id: int,
) -> dict[str, Any]:
    app = await get_or_raise(
        db,
        ColumnApplication,
        ColumnErr.APPLICATION_NOT_FOUND,
        ColumnApplication.id == application_id,
    )

    # 已审幂等：非 PENDING 状态拒绝再次审批（防状态翻转/重复审批）
    if app.status != ColumnApplicationStatus.PENDING:
        raise BizError(ColumnErr.APPLICATION_ALREADY_REVIEWED)

    app.status = info.status
    app.reviewer_id = reviewer_id
    app.review_note = info.review_note
    app.reviewed_at = now_iso()
    await db.flush()

    column = None
    if info.status == ColumnApplicationStatus.APPROVED:
        column = await _ensure_column_for_application(db, app)

    return {
        "application": ColumnApplicationInfo.model_validate(app).model_dump(),
        "column": column.model_dump() if column else None,
    }


async def list_columns(
    db: AsyncSession, page: int = 1, limit: int | None = None
) -> PageData[ColumnInfo]:
    ver = await collection_version("columns")

    async def _load() -> dict[str, Any]:
        total = await db.scalar(select(func.count()).select_from(Column)) or 0
        stmt = select(Column).order_by(Column.id.desc())
        if limit is not None:
            stmt = stmt.offset(paginate_offset(page, limit)).limit(limit)
        cols = (await db.execute(stmt)).scalars().all()
        return PageData(
            items=[ColumnInfo.model_validate(c).model_dump() for c in cols],
            total=total,
            page=page,
            pages=paginate_pages(total, limit) if limit else (1 if total else 0),
        ).model_dump(mode="json")

    payload = await cached_read(
        make_key("columns:list", ver, page, limit), TTL_LIST_S, _load
    )
    # cached_read 返回 PageData 的 dict，转回 schema
    return PageData(
        items=[ColumnInfo.model_validate(c) for c in payload["items"]],
        total=payload["total"],
        page=payload["page"],
        pages=payload["pages"],
    )


async def get_column(db: AsyncSession, column_id: int) -> ColumnInfo:
    async def _load() -> dict[str, Any]:
        return ColumnInfo.model_validate(
            await get_or_raise(
                db,
                Column,
                ColumnErr.NOT_FOUND,
                Column.id == column_id,
            )
        ).model_dump()

    payload = await cached_read(make_key("columns:by_id", column_id), TTL_ITEM_S, _load)
    return ColumnInfo.model_validate(payload)


async def get_column_by_slug(db: AsyncSession, slug: str) -> ColumnInfo:
    async def _load() -> dict[str, Any]:
        return ColumnInfo.model_validate(
            await get_or_raise(
                db,
                Column,
                ColumnErr.NOT_FOUND,
                Column.slug == slug,
            )
        ).model_dump()

    payload = await cached_read(make_key("columns:by_slug", slug), TTL_ITEM_S, _load)
    return ColumnInfo.model_validate(payload)


async def create_post(
    db: AsyncSession, column_id: int, info: ColumnPostCreate, author_id: int
) -> ColumnPostInfo:
    await get_or_raise(db, Column, ColumnErr.NOT_FOUND, Column.id == column_id)

    post = ColumnPost(
        column_id=column_id,
        author_id=author_id,
        title=info.title,
        summary=info.summary,
        content=info.content,
        status=ColumnPostStatus.PUBLISHED,
        published_at=now_iso(),
    )
    db.add(post)
    await db.flush()
    # M0.5.2：专栏原生发帖走独立 column_posts 表（不经统一 content_items），
    # 单独按 column_post_native 计一次产出，避免被漏计。
    post_created_total.labels("column_post_native").inc()
    return ColumnPostInfo.model_validate(post)


async def list_posts(
    db: AsyncSession, column_id: int, page: int = 1, limit: int | None = None
) -> PageData[ColumnPostInfo]:
    await get_or_raise(db, Column, ColumnErr.NOT_FOUND, Column.id == column_id)
    total = (
        await db.scalar(
            select(func.count())
            .select_from(ColumnPost)
            .where(ColumnPost.column_id == column_id)
        )
        or 0
    )
    stmt = (
        select(ColumnPost)
        .where(ColumnPost.column_id == column_id)
        .order_by(ColumnPost.id.desc())
    )
    if limit is not None:
        stmt = stmt.offset(paginate_offset(page, limit)).limit(limit)
    posts = (await db.execute(stmt)).scalars().all()
    return PageData(
        items=[ColumnPostInfo.model_validate(p) for p in posts],
        total=total,
        page=page,
        pages=paginate_pages(total, limit) if limit else (1 if total else 0),
    )


async def get_post(
    db: AsyncSession, post_id: int, column_id: int | None = None
) -> ColumnPostInfo:
    filters = [ColumnPost.id == post_id]
    if column_id is not None:
        filters.append(ColumnPost.column_id == column_id)
    return ColumnPostInfo.model_validate(
        await get_or_raise(
            db,
            ColumnPost,
            ColumnErr.POST_NOT_FOUND,
            *filters,
        )
    )


async def _ensure_column_for_application(
    db: AsyncSession, application: ColumnApplication
) -> ColumnInfo:
    col = (
        (
            await db.execute(
                select(Column).where(Column.application_id == application.id)
            )
        )
        .scalars()
        .first()
    )
    if col:
        return ColumnInfo.model_validate(col)

    col = Column(
        owner_id=application.user_id,
        application_id=application.id,
        title=application.title,
        description=application.description,
    )
    db.add(col)
    await db.flush()
    # 集合新增：升级列列表版本号，使旧分页缓存立即失效（写后读一致）
    await bump_collection_version("columns")
    return ColumnInfo.model_validate(col)


# =====================================================================
# boards(版块) 子域 —— 归入 content 单一 service 命名空间。
# 原实现迁自 app/modules/content/boards/service.py；boards/service.py 现为薄 re-export 桩。
# 表(Board/BoardApplication/BoardBan)与 schemas/errors 保持原样；发言准入
# ``check_post_allowed`` 经 content/service.create_item 懒 import 引用（供测试按
# boards.service 路径 monkeypatch），语义不变。
# =====================================================================


def _board_to_schema(b: Board) -> BoardOut:
    return BoardOut.model_validate(b)


def _bb_application_to_schema(a: BoardApplication) -> BoardApplicationOut:
    return BoardApplicationOut.model_validate(a)


# ————— Board CRUD —————
async def create_board_ex(
    db: AsyncSession, info: BoardCreate, owner_id: int | None
) -> BoardOut:
    conflict = await db.scalar(select(Board.id).where(Board.slug == info.slug))
    if conflict is not None:
        raise BizError(BoardErr.SLUG_CONFLICT)
    board = Board(
        slug=info.slug,
        title=info.title,
        description=info.description,
        owner_id=owner_id,
        parent_id=info.parent_id,
        require_certified=info.require_certified,
        daily_post_limit=info.daily_post_limit,
        is_public=info.is_public,
    )
    db.add(board)
    await db.flush()
    return _board_to_schema(board)


async def list_boards(db: AsyncSession) -> list[BoardOut]:
    rows = (await db.execute(select(Board).order_by(Board.id.asc()))).scalars().all()
    return [_board_to_schema(b) for b in rows]


async def get_board_ex(db: AsyncSession, board_id: int) -> Board:
    return await get_or_raise(db, Board, BoardErr.BOARD_NOT_FOUND, Board.id == board_id)


async def update_board_ex(
    db: AsyncSession,
    board_id: int,
    owner_id: int,
    patch: BoardUpdate,
    *,
    is_admin: bool = False,
) -> BoardOut:
    board = await get_board_ex(db, board_id)
    _assert_owner(board, owner_id, is_admin)
    data = patch.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(board, k, v)
    await db.flush()
    return _board_to_schema(board)


def _assert_owner(board: Board, current_user_id: int, is_admin: bool = False) -> None:
    # 防御性断言：非属主且非 admin(代管) → 拒。路由层 check_owner 已先做对象级
    # 判定(board_owner_manage)，此处 is_admin 由路由传 cur.role=="super_admin" 放行代管。
    if board.owner_id != current_user_id and not is_admin:
        raise BizError(BoardErr.NOT_BOARD_OWNER)


# ————— 板块申请/审核 —————
async def submit_application(
    db: AsyncSession, applicant_id: int, info: BoardApplicationCreate
) -> BoardApplicationOut:
    # slug 为全局唯一命名空间：既不能与已存在的板块冲突，也不能与待审申请冲突
    conflict = await db.scalar(
        select(BoardApplication.id).where(BoardApplication.slug == info.slug)
    )
    if conflict is not None:
        raise BizError(BoardErr.SLUG_CONFLICT)
    board_conflict = await db.scalar(select(Board.id).where(Board.slug == info.slug))
    if board_conflict is not None:
        raise BizError(BoardErr.SLUG_CONFLICT)
    app_ = BoardApplication(
        applicant_id=applicant_id,
        title=info.title,
        description=info.description,
        reason=info.reason,
        slug=info.slug,
        status="pending",
    )
    db.add(app_)
    await db.flush()
    return _bb_application_to_schema(app_)


# 板块申请审核：与 columns 的 review_application 语义/签名不同（都叫同名，
# 迁入 content 单一 service 后须区内唯一名），故加 boards 前缀；boards/service.py
# 以原同名 `review_application` 重导出，保持 boards/router 与测试的既有调用不变。
async def review_board_application(
    db: AsyncSession,
    application_id: int,
    reviewer_id: int,
    body: ReviewBoardApplicationRequest,
) -> BoardApplicationOut:
    app_ = await get_or_raise(
        db,
        BoardApplication,
        BoardErr.APPLICATION_NOT_FOUND,
        BoardApplication.id == application_id,
    )
    if app_.status != "pending":
        raise BizError(BoardErr.APPLICATION_ALREADY_REVIEWED)
    if body.approve:
        # 通过前先核对该申请 slug 是否已被现有 Board 占用；若占用则提前报冲突，
        # 不修改申请状态，避免状态被标记 approved/reviewed 却未真正创建板块的不一致局面。
        board_conflict = await db.scalar(
            select(Board.id).where(Board.slug == app_.slug)
        )
        if board_conflict is not None:
            raise BizError(BoardErr.SLUG_CONFLICT)
    app_.status = "approved" if body.approve else "rejected"
    app_.reviewer_id = reviewer_id
    app_.review_note = body.note
    app_.reviewed_at = now_iso()
    await db.flush()
    if body.approve:
        # 通过则创建板块并把申请人设为负责人；slug 若被占用则报冲突（罕见、明确）
        await create_board_ex(
            db,
            BoardCreate(
                slug=app_.slug,
                title=app_.title,
                description=app_.description,
            ),
            owner_id=app_.applicant_id,
        )
    return _bb_application_to_schema(app_)


# ————— 禁言 —————
async def ban_user(
    db: AsyncSession,
    board: Board,
    actor_id: int,
    body: BanRequest,
    *,
    is_admin: bool = False,
) -> None:
    _assert_owner(board, actor_id, is_admin)
    already = await db.scalar(
        select(BoardBan.id).where(
            BoardBan.board_id == board.id,
            BoardBan.user_id == body.user_id,
            BoardBan.expires_at > now_iso(),
        )
    )
    if already is not None:
        raise BizError(BoardErr.ALREADY_BANNED)
    db.add(
        BoardBan(
            board_id=board.id,
            user_id=body.user_id,
            created_by=actor_id,
            reason=body.reason,
            expires_at=now_iso() + _dt.timedelta(hours=body.hours),
        )
    )
    await db.flush()


async def unban_user(
    db: AsyncSession,
    board: Board,
    actor_id: int,
    target_user_id: int,
    *,
    is_admin: bool = False,
) -> None:
    _assert_owner(board, actor_id, is_admin)
    await db.execute(
        sa_delete(BoardBan).where(
            BoardBan.board_id == board.id,
            BoardBan.user_id == target_user_id,
        )
    )
    await db.flush()


async def is_banned(db: AsyncSession, board_id: int, user_id: int) -> bool:
    row = await db.scalar(
        select(BoardBan.id).where(
            BoardBan.board_id == board_id,
            BoardBan.user_id == user_id,
            BoardBan.expires_at > now_iso(),
        )
    )
    return row is not None


# ————— 发言准入（供 forum create_post 调用）—————
async def check_post_allowed(db: AsyncSession, board_id: int, user_id: int) -> None:
    """校验用户在板块的发帖资格：板块存在 / 可见 / 未禁言 / 认证 / 日限发。异常抛相应 BoardErr。"""
    board = await get_board_ex(db, board_id)
    if not board.is_public:
        # 私有板块：需 normal 以上（认证成员）。account_level 判给 auth 快照缝（含 banned 态）。
        _snap = await get_user_snapshot(db, user_id=user_id)
        ulevel = _snap.account_level if _snap else None
        if ulevel not in ("normal", "admin"):
            raise BizError(BoardErr.BOARD_NOT_PUBLIC)
    if await is_banned(db, board.id, user_id):
        raise BizError(BoardErr.BOARD_BANNED)
    if board.require_certified:
        passed = await db.scalar(
            select(ExamCertificate.id)
            .join(Exam, Exam.id == ExamCertificate.exam_id)
            .where(
                # 初级通识考试通过判定：证书来自 type=exam 且 unlock_level=normal 的认证考试
                ExamCertificate.user_id == user_id,
                ExamCertificate.passed.is_(True),
                Exam.type == "exam",
                Exam.unlock_level == "normal",
            )
            .limit(1)
        )
        if passed is None:
            raise BizError(BoardErr.CERTIFICATION_REQUIRED)
    if board.daily_post_limit > 0:
        today_start = now_iso().replace(hour=0, minute=0, second=0, microsecond=0)
        cnt = (
            await db.scalar(
                select(func.count(ContentItem.id)).where(
                    ContentItem.author_id == user_id,
                    ContentItem.board_id == board_id,
                    ContentItem.content_type == ContentType.DISCUSSION,
                    ContentItem.created_at >= today_start,
                )
            )
            or 0
        )
        if cnt >= board.daily_post_limit:
            raise BizError(BoardErr.DAILY_POST_LIMIT_REACHED)


# =====================================================================
# qa(问答) 子域 —— 归入 content 单一 service 命名空间。
# 原实现迁自 app/modules/content/qa/service.py；qa/service.py 现为薄 re-export 桩。
# 独立表(qa_questions/qa_answers/qa_question_images)与 schemas/errors 保持原样；
# escrow 锁定/采纳派发/退回语义不变，只外壳迁址收口。
# =====================================================================


async def create_question(
    db: AsyncSession, author_id: int, info: QuestionCreate
) -> QuestionOut:
    """发问：spend 锁定总悬赏 + 写 Question（同事务）。"""
    total = info.bounty_people * info.bounty_per_person
    # 先建 Question 拿 id（作为 spend 的 ref_id）
    q = QAQuestion(
        author_id=author_id,
        title=info.title,
        situation=info.situation,
        content=info.content,
        category=info.category,
        bounty_people=info.bounty_people,
        bounty_per_person=info.bounty_per_person,
        bounty_total=total,
        bounty_distributed=0,
        status="open",
    )
    db.add(q)
    await db.flush()
    if total > 0:
        # spend 锁定（余额不足抛 INSUFFICIENT_BALANCE，同事务回滚）
        await spend(db, author_id, total, "qa_escrow", "qa_question", str(q.id))
    if info.images:
        for i, url in enumerate(info.images):
            db.add(QAQuestionImage(question_id=q.id, url=url, sort=i))
    # 论坛可见：QA 提问同步落一条 content_items（content_type='qa'，挂 qa board）
    await _sync_question_content_item(db, author_id, q)
    await db.flush()
    await bump_collection_version("qa")
    names = await _qa_author_names(db, [q.author_id])
    return _question_to_schema(q, names.get(q.author_id, ""))


async def _ensure_qa_board(db: AsyncSession) -> int:
    """确保存在 qa 板块（统一分类轴），QA 提问的论坛条目挂到它。"""
    existing = await db.scalar(select(Board.id).where(Board.slug == "qa"))
    if existing is not None:
        return existing
    board = Board(slug="qa", title="问答", description="用户提问与解答")
    db.add(board)
    await db.flush()
    return board.id


async def _sync_question_content_item(
    db: AsyncSession, author_id: int, q: QAQuestion
) -> None:
    """QA 提问同步为论坛可见条目（content_items，content_type='qa'）。"""
    board_id = await _ensure_qa_board(db)
    item = ContentItem(
        content_type="qa",
        board_id=board_id,
        author_id=author_id,
        qa_question_id=q.id,
        title=q.title,
        excerpt=_qa_plain(q.content or q.situation),
        content=q.content,
        tags="[]",
        status="published",
    )
    db.add(item)
    await db.flush()
    # M0.5.2：QA 提问同步为论坛可见的 content_items 条目，视作一次 qa 类产出。
    post_created_total.labels("qa").inc()
    # QA 提问同步落成 content_items 会增加统一内容列表的计数，需 bump content 集合版本
    await bump_collection_version("content")


def _qa_plain(text: str, limit: int = 150) -> str:
    """提取纯文本摘要（去空白/HTML 残留）。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:limit].rstrip() + ("..." if len(t) > limit else "")


async def _qa_author_names(db: AsyncSession, author_ids: list[int]) -> dict[int, str]:
    """批量取作者展示名（id → 昵称/用户名），委托 auth 只读缝批次读。
    """
    ids = {i for i in author_ids if i}
    if not ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(ids))
    return {uid: s.display_name for uid, s in snaps.items()}


async def list_questions(
    db: AsyncSession,
    page: int = 1,
    limit: int = 20,
    category: str | None = None,
) -> PageData[QuestionOut]:
    async def load() -> list[dict[str, Any]]:
        base = select(QAQuestion, func.count(QAAnswer.id)).outerjoin(
            QAAnswer, QAAnswer.question_id == QAQuestion.id
        )
        if category:
            base = base.where(QAQuestion.category == category)
        rows = (
            await db.execute(
                base.group_by(QAQuestion.id)
                .order_by(QAQuestion.id.desc())
                .offset(paginate_offset(page, limit))
                .limit(limit)
            )
        ).all()
        questions = [q for q, _ in rows]
        names = await _qa_author_names(db, [q.author_id for q in questions])
        return [
            QuestionOut.model_validate(q)
            .model_copy(
                update={
                    "answer_count": ans_count,
                    "author_name": names.get(q.author_id, ""),
                }
            )
            .model_dump()
            for q, ans_count in rows
        ]

    ver = await collection_version("qa")
    payload = await cached_read(
        make_key("qa:list", ver, page, limit, category or ""), 60, load
    )
    # 分页元信息（total 单独查，不缓存）
    total_where = [QAQuestion.category == category] if category else []
    total = await db.scalar(select(func.count(QAQuestion.id)).where(*total_where)) or 0
    return PageData(
        items=[QuestionOut.model_validate(p) for p in payload],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def get_question(db: AsyncSession, question_id: int) -> QuestionDetail:
    q = await get_or_raise(
        db, QAQuestion, QaErr.QUESTION_NOT_FOUND, QAQuestion.id == question_id
    )
    answers = (
        (
            await db.execute(
                select(QAAnswer)
                .where(QAAnswer.question_id == question_id)
                .order_by(QAAnswer.id.asc())
            )
        )
        .scalars()
        .all()
    )
    images = (
        (
            await db.execute(
                select(QAQuestionImage)
                .where(QAQuestionImage.question_id == question_id)
                .order_by(QAQuestionImage.sort.asc())
            )
        )
        .scalars()
        .all()
    )
    names = await _qa_author_names(db, [q.author_id])
    base = QuestionOut.model_validate(q).model_copy(
        update={
            "answer_count": len(answers),
            "author_name": names.get(q.author_id, ""),
        }
    )
    return QuestionDetail(
        **base.model_dump(),
        answers=[AnswerOut.model_validate(a) for a in answers],
        images=[img.url for img in images],
    )


async def create_answer(
    db: AsyncSession, question_id: int, author_id: int, info: AnswerCreate
) -> AnswerOut:
    q = await get_or_raise(
        db, QAQuestion, QaErr.QUESTION_NOT_FOUND, QAQuestion.id == question_id
    )
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    a = QAAnswer(question_id=question_id, author_id=author_id, content=info.content)
    db.add(a)
    await db.flush()
    await bump_collection_version("qa")
    return AnswerOut.model_validate(a)


async def accept_answer(
    db: AsyncSession, question_id: int, answer_id: int, asker_id: int
) -> AnswerOut:
    """发问者采纳回答：防超发派发人均积分给回答者（同事务）。"""
    q = await get_or_raise(
        db, QAQuestion, QaErr.QUESTION_NOT_FOUND, QAQuestion.id == question_id
    )
    if q.author_id != asker_id:
        raise BizError(QaErr.NOT_ASKER)
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    a = await get_or_raise(
        db,
        QAAnswer,
        QaErr.ANSWER_NOT_FOUND,
        QAAnswer.id == answer_id,
        QAAnswer.question_id == question_id,
    )
    if a.is_accepted:
        return AnswerOut.model_validate(a)
    # 防超发：已采纳数 >= 悬赏人数 → 拒
    accepted_count = (
        await db.scalar(
            select(func.count(QAAnswer.id)).where(
                QAAnswer.question_id == question_id,
                QAAnswer.is_accepted.is_(True),
            )
        )
        or 0
    )
    if accepted_count >= q.bounty_people:
        raise BizError(QaErr.BOUNTY_EXHAUSTED)
    if q.bounty_per_person > 0:
        await reward(
            db,
            a.author_id,
            q.bounty_per_person,
            "qa_accept",
            "qa_accept",
            f"{question_id}:{answer_id}",
        )
        q.bounty_distributed += q.bounty_per_person
    a.is_accepted = True
    q.accepted_answer_id = a.id
    await db.flush()
    # 采纳回答事件入队（仅计数，QA 已按悬赏派发，不加分）
    await enqueue_points_event(db, a.author_id, "answer_accepted", f"answer:{a.id}")
    await bump_collection_version("qa")
    return AnswerOut.model_validate(a)


async def close_question(
    db: AsyncSession,
    question_id: int,
    asker_id: int,
    accepted_answer_id: int | None = None,
) -> QuestionOut:
    """发问者关闭问题：可同时采纳一个回答；剩余 escrow 退回发问者。"""
    q = await get_or_raise(
        db, QAQuestion, QaErr.QUESTION_NOT_FOUND, QAQuestion.id == question_id
    )
    if q.author_id != asker_id:
        raise BizError(QaErr.NOT_ASKER)
    if q.status != "open":
        raise BizError(QaErr.QUESTION_NOT_OPEN)
    if accepted_answer_id is not None and accepted_answer_id != q.accepted_answer_id:
        await accept_answer(db, question_id, accepted_answer_id, asker_id)
        # 重新载入 q（accept 改了 distributed）
        q = await get_or_raise(
            db,
            QAQuestion,
            QaErr.QUESTION_NOT_FOUND,
            QAQuestion.id == question_id,
        )
    # 剩余 escrow 退回发问者
    refund = q.bounty_total - q.bounty_distributed
    if refund > 0:
        await reward(db, q.author_id, refund, "qa_refund", "qa_refund", str(q.id))
    q.status = "accepted" if q.accepted_answer_id is not None else "closed"
    await db.flush()
    await bump_collection_version("qa")
    names = await _qa_author_names(db, [q.author_id])
    return _question_to_schema(q, names.get(q.author_id, ""))


def _question_to_schema(q: QAQuestion, author_name: str = "") -> QuestionOut:
    out = QuestionOut.model_validate(q)
    if author_name:
        return out.model_copy(update={"author_name": author_name})
    return out
