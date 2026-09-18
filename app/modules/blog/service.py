import asyncio
import hashlib
import uuid
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError, CommonErr
from app.db.base import now_iso
from app.db.repo import get_or_raise
from app.modules.auth.schemas import ProfileInfo
from app.modules.auth.snapshot import (
    get_user_snapshot,
    get_user_snapshot_batch,
    profile_info_from_snap,
)
from app.modules.blog import git_svc
from app.modules.blog.errors import BlogErr
from app.modules.blog.models import (
    BlogComment,
    BlogContent,
    BlogRepoQuarantine,
    BlogSeries,
    BlogStar,
)
from app.modules.blog.schemas import (
    BlogCommentCreate,
    BlogCommentInfo,
    BlogSeriesCreate,
    BlogSeriesDetail,
    BlogSeriesInfo,
    BlogSeriesUpdate,
    BlogStarStatus,
)
from app.modules.content.models import Board
from app.modules.content.service import publish_blog_item

# ---- private converters ----


def _series_to_info(
    s: BlogSeries, star_count: int = 0, is_starred: bool = False
) -> BlogSeriesInfo:
    return BlogSeriesInfo.model_validate(s).model_copy(
        update={"star_count": star_count, "is_starred": is_starred}
    )


def _comment_to_info(
    c: BlogComment, profile: ProfileInfo | None = None
) -> BlogCommentInfo:
    return BlogCommentInfo.model_validate(c).model_copy(update={"profile": profile})


# ---- star helpers ----


async def _star_count(db: AsyncSession, series_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.count(BlogStar.user_id)).where(BlogStar.series_id == series_id)
        )
        or 0
    )


async def _is_starred(
    db: AsyncSession, series_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    return (
        await db.execute(
            select(BlogStar).where(
                BlogStar.series_id == series_id, BlogStar.user_id == user_id
            )
        )
    ).scalars().first() is not None


async def _star_counts(
    db: AsyncSession, series_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """批量统计多个系列的 star 数量，避免逐条查询的 N+1。"""
    if not series_ids:
        return {}
    rows = (
        await db.execute(
            select(BlogStar.series_id, func.count(BlogStar.user_id))
            .where(BlogStar.series_id.in_(set(series_ids)))
            .group_by(BlogStar.series_id)
        )
    ).all()
    return {sid: cnt for sid, cnt in rows}


async def _starred_ids(
    db: AsyncSession, series_ids: list[uuid.UUID], user_id: uuid.UUID
) -> set[uuid.UUID]:
    """批量查当前用户 star 了哪些系列，避免逐条查询的 N+1。"""
    if not series_ids:
        return set()
    rows = (
        await db.execute(
            select(BlogStar.series_id).where(
                BlogStar.series_id.in_(set(series_ids)), BlogStar.user_id == user_id
            )
        )
    ).all()
    return {sid for (sid,) in rows}


async def _get_profile(db: AsyncSession, user_id: uuid.UUID) -> ProfileInfo | None:
    """评论作者 ProfileInfo，经 auth 读缝取（M3.A残项：不再直读 Profile）。"""
    snap = await get_user_snapshot(db, user_id=user_id)
    if snap is None:
        return None
    return profile_info_from_snap(snap)


async def _get_profiles(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProfileInfo | None]:
    """批量取评论作者 ProfileInfo（经 auth 批量读缝一次查齐，避免逐条补 profile 的散读）。"""
    snaps = await get_user_snapshot_batch(db, user_ids=list(user_ids))
    return {uid: profile_info_from_snap(snaps[uid]) for uid in snaps}


def _sha3(content: str) -> str:
    """计算正文 sha3-256 指纹，用于内容变更检测。"""
    return hashlib.sha3_256(content.encode("utf-8")).hexdigest()


# 文件树中间节点：name→(嵌套子树 dict | "__BLOB__" 终端标记)，与 git_svc.TreeNode 同构
_FileTreeNode = dict[str, "_FileTreeNode | str"]


def _paths_to_file_tree(paths: list[str]) -> list[dict[str, Any]]:
    """由文件的路径列表构建嵌套文件树，结构与原 git ``ls-tree`` 输出一致。

    返回节点形如 ``{"name", "type", "children"?}``：目录 type=tree 带 children，
    文件 type=blob；目录/文件同级按 name 排序。复用与 ``git_svc.TreeNode`` 同构的
    中间结构以通过严格类型检查。
    """
    root: _FileTreeNode = {}

    def _ensure(node: _FileTreeNode, parts: list[str]) -> None:
        if not parts:
            return
        name, rest = parts[0], parts[1:]
        cur = node.get(name)
        if not isinstance(cur, dict):
            cur = {}
            node[name] = cur
        if not rest:
            node[name] = "__BLOB__"
        else:
            _ensure(cur, rest)

    for p in paths:
        _ensure(root, p.strip("/").split("/"))

    def _to_list(node: _FileTreeNode) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name in sorted(node):
            val = node[name]
            if isinstance(val, dict):
                result.append(
                    {
                        "name": name,
                        "type": "tree",
                        "children": _to_list(val),
                    }
                )
            else:
                result.append({"name": name, "type": "blob"})
        return result

    return _to_list(root)


async def _get_content_row(
    db: AsyncSession, series_id: uuid.UUID, filepath: str
) -> BlogContent:
    row = (
        (
            await db.execute(
                select(BlogContent).where(
                    BlogContent.series_id == series_id,
                    BlogContent.path == filepath,
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise BizError(BlogErr.FILE_NOT_FOUND, f"File not found: {filepath}")
    return row


# ---- series CRUD ----


async def create_series(
    db: AsyncSession, user_id: uuid.UUID, info: BlogSeriesCreate
) -> BlogSeriesInfo:
    existing = (
        (
            await db.execute(
                select(BlogSeries).where(BlogSeries.repo_name == info.repo_name)
            )
        )
        .scalars()
        .first()
    )
    if existing:
        raise BizError(CommonErr.INVALID_INPUT, "Repository name already taken")

    # git 子进程同步调用放在线程池执行，避免阻塞事件循环
    await asyncio.to_thread(git_svc.init_bare_repo, info.repo_name)

    series = BlogSeries(
        owner_id=user_id,
        title=info.title,
        description=info.description,
        cover_url=info.cover_url,
        repo_name=info.repo_name,
    )
    db.add(series)
    await db.flush()
    return _series_to_info(series)


async def list_series(
    db: AsyncSession,
    current_user_id: uuid.UUID | None = None,
    page: int = 1,
    limit: int | None = None,
) -> PageData[BlogSeriesInfo]:
    """
    系列列表，统一返回 ``PageData``。不传 ``limit`` 时返回全部（page 恒为 1，pages 视总数），
    传了则在 SQL 层分页，避免大数据量时整表拉取。
    """
    total = await db.scalar(select(func.count()).select_from(BlogSeries)) or 0
    stmt = select(BlogSeries).order_by(BlogSeries.id.desc())
    if limit is not None:
        stmt = stmt.offset(paginate_offset(page, limit)).limit(limit)
    items = (await db.execute(stmt)).scalars().all()
    ids = [s.id for s in items]
    counts = await _star_counts(db, ids)
    starred_ids = (
        await _starred_ids(db, ids, current_user_id)
        if current_user_id
        else set[uuid.UUID]()
    )
    return PageData(
        items=[
            _series_to_info(
                s, star_count=counts.get(s.id, 0), is_starred=s.id in starred_ids
            )
            for s in items
        ],
        total=total,
        page=page,
        pages=paginate_pages(total, limit) if limit else (1 if total else 0),
    )


async def get_series(
    db: AsyncSession, series_id: uuid.UUID, current_user_id: uuid.UUID | None = None
) -> BlogSeriesDetail:
    series = await get_or_raise(
        db,
        BlogSeries,
        BlogErr.SERIES_NOT_FOUND,
        BlogSeries.id == series_id,
    )

    sc = await _star_count(db, series_id)
    starred = (
        await _is_starred(db, series_id, current_user_id) if current_user_id else False
    )

    file_tree: list[dict[str, Any]] | None = None
    rows = (
        (
            await db.execute(
                select(BlogContent.path).where(BlogContent.series_id == series_id)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        file_tree = _paths_to_file_tree(list(rows))

    return BlogSeriesDetail.model_validate(series).model_copy(
        update={"star_count": sc, "is_starred": starred, "file_tree": file_tree}
    )


async def update_series(
    db: AsyncSession, series_id: uuid.UUID, user_id: uuid.UUID, info: BlogSeriesUpdate
) -> BlogSeriesInfo:
    series = await get_or_raise(
        db,
        BlogSeries,
        BlogErr.SERIES_NOT_FOUND,
        BlogSeries.id == series_id,
    )
    if series.owner_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)

    if info.title is not None:
        series.title = info.title
    if info.description is not None:
        series.description = info.description
    if info.cover_url is not None:
        series.cover_url = info.cover_url
    if info.status is not None:
        series.status = info.status
    series.updated_at = now_iso()

    await db.flush()
    return _series_to_info(series)


async def delete_series(
    db: AsyncSession, series_id: uuid.UUID, user_id: uuid.UUID, as_admin: bool = False
) -> uuid.UUID:
    series = await get_or_raise(
        db,
        BlogSeries,
        BlogErr.SERIES_NOT_FOUND,
        BlogSeries.id == series_id,
    )
    if not as_admin and series.owner_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)

    await asyncio.to_thread(git_svc.delete_repo, series.repo_name)
    # 正常删除系列时，同步清理可能的隔离台账(幂等:无则忽略)
    qrow = (
        (
            await db.execute(
                select(BlogRepoQuarantine).where(
                    BlogRepoQuarantine.repo_name == series.repo_name
                )
            )
        )
        .scalars()
        .first()
    )
    if qrow is not None:
        await db.delete(qrow)
    owner_id = series.owner_id
    await db.delete(series)
    await db.flush()
    return owner_id


async def toggle_star(
    db: AsyncSession, series_id: uuid.UUID, user_id: uuid.UUID
) -> BlogStarStatus:
    await get_or_raise(
        db, BlogSeries, BlogErr.SERIES_NOT_FOUND, BlogSeries.id == series_id
    )

    existing = (
        (
            await db.execute(
                select(BlogStar).where(
                    BlogStar.series_id == series_id, BlogStar.user_id == user_id
                )
            )
        )
        .scalars()
        .first()
    )

    if existing:
        await db.delete(existing)
        await db.flush()
        return BlogStarStatus(
            starred=False, star_count=await _star_count(db, series_id)
        )

    star = BlogStar(user_id=user_id, series_id=series_id)
    db.add(star)
    await db.flush()
    return BlogStarStatus(starred=True, star_count=await _star_count(db, series_id))


# ---- comments ----


async def create_comment(
    db: AsyncSession, series_id: uuid.UUID, user_id: uuid.UUID, info: BlogCommentCreate
) -> BlogCommentInfo:
    await get_or_raise(
        db, BlogSeries, BlogErr.SERIES_NOT_FOUND, BlogSeries.id == series_id
    )

    if info.parent_id is not None:
        parent = await get_or_raise(
            db,
            BlogComment,
            CommonErr.INVALID_INPUT,
            BlogComment.id == info.parent_id,
        )
        if parent.series_id != series_id:
            raise BizError(CommonErr.INVALID_INPUT, "Parent comment not found")

    comment = BlogComment(
        user_id=user_id,
        series_id=series_id,
        content=info.content,
        parent_id=info.parent_id,
    )
    db.add(comment)
    await db.flush()
    # 重新用 selectinload 预载 replies，避免序列化时懒加载触发 MissingGreenlet
    loaded_comment = (
        (
            await db.execute(
                select(BlogComment)
                .where(BlogComment.id == comment.id)
                .options(selectinload(BlogComment.replies))
            )
        )
        .scalars()
        .first()
    )
    if loaded_comment is None:
        loaded_comment = comment
    return _comment_to_info(loaded_comment, profile=await _get_profile(db, user_id))


async def list_comments(
    db: AsyncSession, series_id: uuid.UUID
) -> list[BlogCommentInfo]:
    await get_or_raise(
        db, BlogSeries, BlogErr.SERIES_NOT_FOUND, BlogSeries.id == series_id
    )

    comments = (
        (
            await db.execute(
                select(BlogComment)
                .where(BlogComment.series_id == series_id)
                .order_by(BlogComment.created_at.asc())
                .options(selectinload(BlogComment.replies))
            )
        )
        .scalars()
        .all()
    )

    user_ids = {c.user_id for c in comments}
    profiles = await _get_profiles(db, user_ids)

    comment_map: dict[uuid.UUID, BlogCommentInfo] = {}
    roots: list[BlogCommentInfo] = []

    for c in comments:
        info = _comment_to_info(c, profile=profiles.get(c.user_id))
        comment_map[c.id] = info

    for c in comments:
        info = comment_map[c.id]
        if c.parent_id is not None and c.parent_id in comment_map:
            comment_map[c.parent_id].replies.append(info)
        else:
            roots.append(info)

    return roots


async def delete_comment(
    db: AsyncSession,
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    user_id: uuid.UUID,
    as_admin: bool = False,
) -> uuid.UUID:
    comment = await get_or_raise(
        db,
        BlogComment,
        BlogErr.COMMENT_NOT_FOUND,
        BlogComment.id == comment_id,
        BlogComment.series_id == series_id,
    )
    if not as_admin and comment.user_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)
    author_id = comment.user_id
    await db.delete(comment)
    await db.flush()
    return author_id


# ---- files ----


async def get_file_content(
    db: AsyncSession, series_id: uuid.UUID, filepath: str
) -> dict[str, Any]:
    await get_or_raise(
        db,
        BlogSeries,
        BlogErr.SERIES_NOT_FOUND,
        BlogSeries.id == series_id,
    )
    filepath = filepath.lstrip("/") or filepath
    row = await _get_content_row(db, series_id, filepath)
    return {"filepath": row.path, "content": row.content}


async def write_series_file(
    db: AsyncSession,
    series_id: uuid.UUID,
    user_id: uuid.UUID,
    filepath: str,
    content: str,
    message: str | None = None,
) -> None:
    series = await get_or_raise(
        db,
        BlogSeries,
        BlogErr.SERIES_NOT_FOUND,
        BlogSeries.id == series_id,
    )
    if series.owner_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)

    filepath = filepath.lstrip("/") or filepath
    row = (
        (
            await db.execute(
                select(BlogContent).where(
                    BlogContent.series_id == series_id,
                    BlogContent.path == filepath,
                )
            )
        )
        .scalars()
        .first()
    )
    new_sha = _sha3(content)
    if row is None:
        row = BlogContent(
            series_id=series_id,
            path=filepath,
            content=content,
            sha3=new_sha,
            version=1,
        )
        db.add(row)
    else:
        # 内容是否变化：仅当 sha3 变化才递增 version，避免无意义的重写
        if row.sha3 != new_sha:
            row.content = content
            row.sha3 = new_sha
            row.version = row.version + 1
            row.updated_at = now_iso()

    series.updated_at = now_iso()
    await db.flush()


# ---- publish ----


async def _ensure_board(db: AsyncSession, slug: str) -> uuid.UUID:
    """blog 发布时按 slug 解析板块（统一分类轴）；不存在则自动建并返回 board_id。"""
    existing = await db.scalar(select(Board.id).where(Board.slug == slug))
    if existing is not None:
        return existing
    board = Board(slug=slug, title=slug, description="auto-created for blog publish")
    db.add(board)
    await db.flush()
    return board.id


async def publish_series_file(
    db: AsyncSession,
    series_id: uuid.UUID,
    user_id: uuid.UUID,
    filepath: str,
    override: dict[str, Any] | None = None,
) -> uuid.UUID:
    """把 series 指定 MDX 读出来、解析 frontmatter、落库为 content_items（blog_post，幂等更新）。

    返回统一内容项 id；category 前端自由标签映射为 boards（get-or-create）。
    """
    series = await get_or_raise(
        db, BlogSeries, BlogErr.SERIES_NOT_FOUND, BlogSeries.id == series_id
    )
    if series.owner_id != user_id:
        raise BizError(CommonErr.FORBIDDEN)

    filepath = filepath.lstrip("/") or filepath
    row = await _get_content_row(db, series_id, filepath)
    content = row.content
    fm = git_svc.parse_frontmatter(content)
    override = override or {}

    raw_slug = override.get("slug") or fm.get("slug") or filepath.split("/")[-1]
    slug = str(raw_slug).removesuffix(".mdx").removesuffix(".md")
    first_line = content.split("\n", 1)[0].replace("# ", "").strip()
    title = str(override.get("title") or fm.get("title") or first_line or slug)
    category_slug = str(override.get("category") or fm.get("category") or "blog")
    tags = [
        str(t) for t in cast("list[Any]", override.get("tags") or fm.get("tags") or [])
    ]
    description = override.get("description") or fm.get("description")

    board_id = await _ensure_board(db, category_slug)
    return await publish_blog_item(
        db,
        user_id,
        board_id=board_id,
        slug=slug,
        title=title,
        content=content,
        summary=description,
        cover=None,
        tags=tags,
    )
