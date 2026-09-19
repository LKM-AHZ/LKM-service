import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import respond
from app.db.session import get_session
from app.modules.blog.schemas import (
    BlogCommentCreate,
    BlogCommentInfo,
    BlogSeriesCreate,
    BlogSeriesInfo,
    BlogSeriesUpdate,
    BlogStarStatus,
    SeriesFileWrite,
    SeriesPublish,
)
from app.modules.blog.service import (
    create_comment,
    create_series,
    delete_comment,
    delete_series,
    publish_series_file,
    toggle_star,
    update_series,
    write_series_file,
)
from app.modules.content.schemas import ContentItemInfo
from app.modules.content.service import get_item
from auth.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/blog", tags=["blog"])


# ---- Series ----


@router.post("/series", response_model=ApiResp[BlogSeriesInfo])
@respond
async def create_blog_series(
    info: BlogSeriesCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BlogSeriesInfo:
    return await create_series(db, cur.id, info)


@router.put("/series/{series_id}", response_model=ApiResp[BlogSeriesInfo])
@respond
async def update_blog_series(
    series_id: uuid.UUID,
    info: BlogSeriesUpdate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BlogSeriesInfo:
    return await update_series(db, series_id, cur.id, info)


@router.delete("/series/{series_id}")
@respond
async def delete_blog_series(
    series_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await delete_series(db, series_id, cur.id)
    return None


# ---- Files ----


@router.put(
    "/series/{series_id}/files/{filepath:path}",
    response_model=ApiResp[None],
)
@respond
async def put_blog_file(
    series_id: uuid.UUID,
    filepath: str,
    body: SeriesFileWrite,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await write_series_file(db, series_id, cur.id, filepath, body.content, body.message)
    return None


@router.post("/series/{series_id}/publish", response_model=ApiResp[ContentItemInfo])
@respond
async def publish_series_file_endpoint(
    series_id: uuid.UUID,
    body: SeriesPublish,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ContentItemInfo:
    content_id = await publish_series_file(
        db, series_id, cur.id, body.filepath, body.override
    )
    return await get_item(db, content_id)


# ---- Stars ----


@router.post("/series/{series_id}/star", response_model=ApiResp[BlogStarStatus])
@respond
async def star_blog_series(
    series_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BlogStarStatus:
    return await toggle_star(db, series_id, cur.id)


# ---- Comments ----


@router.post(
    "/series/{series_id}/comments",
    response_model=ApiResp[BlogCommentInfo],
)
@respond
async def create_blog_comment(
    series_id: uuid.UUID,
    info: BlogCommentCreate,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BlogCommentInfo:
    return await create_comment(db, series_id, cur.id, info)


@router.delete("/series/{series_id}/comments/{comment_id}")
@respond
async def delete_blog_comment(
    series_id: uuid.UUID,
    comment_id: uuid.UUID,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await delete_comment(db, series_id, comment_id, cur.id)
    return None
