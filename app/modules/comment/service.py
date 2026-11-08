"""评论（comment）域服务：评论区的发/列/赞/取消赞/软删。

步骤 9 切片 2 自 ``content.service`` 迁出，业务语义逐字保留：

- 发评论前先锁帖子行（串行分配楼层，含软删楼层也不重号），楼层号取「含已软删」的最大值 +1；
- 点赞幂等，积分事件仍用 ``"like"`` + ``comment:{id}`` 前缀（按 ledger ref 幂等）；
- 软删同步递减帖子的 ``comment_count``，口径与对账一致（``deleted_at IS NULL``）。

与 content 的耦合全部经端口：锁行并断言存在、评论数写穿，都走 ``core.ports.content``。
"""

from __future__ import annotations

import uuid

from app.contracts.comment import ContentCommentInfo
from app.modules.comment.counters import bump_like_count
from app.modules.comment.errors import CommentErr
from app.modules.comment.models import ContentComment
from app.modules.comment.repository import (
    ContentCommentLikeRepository,
    ContentCommentRepository,
)
from app.modules.comment.schemas import ContentCommentCreate
from core.common import PageData, paginate_offset, paginate_pages
from core.db.repository import DbSession
from core.ports import content as content_port
from core.ports import points as points_port
from core.ports.snapshot import get_user_display_names as _author_map


def _comment_to_schema(
    c: ContentComment, author_name: str, liked: bool = False
) -> ContentCommentInfo:
    return ContentCommentInfo.model_validate(c).model_copy(
        update={"author_name": author_name, "liked": liked}
    )


async def _liked_comment_ids(
    db: DbSession, comments: list[ContentComment], viewer_id: uuid.UUID | None
) -> set[uuid.UUID]:
    """批量取「viewer 点过赞的评论 id」；未登录时直接空集，不查库。"""
    if viewer_id is None or not comments:
        return set()
    return await ContentCommentLikeRepository(db).liked_comment_ids(
        comment_ids=[c.id for c in comments], user_id=viewer_id
    )


async def _assert_comment_in_item(
    db: DbSession, item_id: uuid.UUID, comment_id: uuid.UUID
) -> ContentComment:
    """取评论并断言它确实属于该内容（不存在/已软删 → COMMENT_NOT_FOUND）。

    路由把 ``item_id`` 与 ``comment_id`` 都放在路径里，若只按 comment_id 查，调用方拿
    别的内容的 item_id 拼出的 URL 也能命中同一条评论——计数与删除都会落在错误的帖子上。
    """
    return await ContentCommentRepository(db).get_one_or_raise(
        CommentErr.COMMENT_NOT_FOUND,
        ContentComment.id == comment_id,
        ContentComment.content_id == item_id,
    )


async def list_comments(
    db: DbSession,
    item_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
    viewer_id: uuid.UUID | None = None,
) -> PageData[ContentCommentInfo]:
    await content_port.require_content_item(db, item_id)
    repo = ContentCommentRepository(db)
    total = await repo.count_in_content(item_id)
    comments = await repo.list_in_content(
        item_id, offset=paginate_offset(page, limit), limit=limit
    )
    names = await _author_map(db, [c.user_id for c in comments])
    liked = await _liked_comment_ids(db, comments, viewer_id)
    items = [
        _comment_to_schema(c, names.get(c.user_id, ""), c.id in liked) for c in comments
    ]
    return PageData(
        items=items,
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def list_all_comments(
    db: DbSession, item_id: uuid.UUID, viewer_id: uuid.UUID | None = None
) -> list[ContentCommentInfo]:
    """一次取回某内容的全部评论（floor 升序），供 GraphQL 组装评论树。"""
    await content_port.require_content_item(db, item_id)
    comments = await ContentCommentRepository(db).list_all_in_content(item_id)
    names = await _author_map(db, [c.user_id for c in comments])
    liked = await _liked_comment_ids(db, comments, viewer_id)
    return [
        _comment_to_schema(c, names.get(c.user_id, ""), c.id in liked) for c in comments
    ]


async def create_comment(
    db: DbSession,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    info: ContentCommentCreate,
) -> ContentCommentInfo:
    # 同一内容的评论必须串行分配楼层；锁持续到事务提交，含软删楼层也不会重号。
    await content_port.lock_content_item(db, item_id)
    repo = ContentCommentRepository(db)
    if info.parent_id is not None:
        await repo.get_one_or_raise(
            CommentErr.COMMENT_NOT_FOUND,
            ContentComment.id == info.parent_id,
            ContentComment.content_id == item_id,
        )

    # 楼层号取「含已软删」的最大值 +1：软删后新评论不得复用可见楼层号
    next_floor = (await repo.max_floor_number(item_id) or 0) + 1

    comment = await repo.add(
        ContentComment(
            content_id=item_id,
            user_id=user_id,
            content=info.content,
            floor_number=next_floor,
            parent_id=info.parent_id,
        )
    )
    await content_port.bump_comment_count(db, item_id, 1)
    await points_port.enqueue_event(db, user_id, "comment", f"comment:{comment.id}")

    names = await _author_map(db, [comment.user_id])
    return _comment_to_schema(comment, names.get(comment.user_id, ""))


async def like_comment(
    db: DbSession, item_id: uuid.UUID, comment_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """点赞评论（幂等）。返回评论的最新点赞数。

    与 ``content.like_item`` 同款：先锁帖子行串行化同一帖下的并发互动，再查明细，重复请求
    不重复计数。

    积分事件与帖子点赞同用 ``"like"``、靠 ref_id 前缀区分目标（``comment:{id}`` vs
    ``item:{id}``）；积分入账按 ledger ref 幂等（见 points/tasks.py::apply_point_reward），
    反复赞/取消不会重复发分。
    """
    await content_port.lock_content_item(db, item_id)
    comment = await _assert_comment_in_item(db, item_id, comment_id)
    repo = ContentCommentLikeRepository(db)
    if await repo.get_one_like(comment_id=comment_id, user_id=user_id) is not None:
        # 帖子行锁已持有，本事务内读到的 like_count 不会被同帖的并发点赞越过
        return comment.like_count

    await repo.create(comment_id=comment_id, user_id=user_id)
    await points_port.enqueue_event(db, user_id, "like", f"comment:{comment_id}")
    return await bump_like_count(db, comment_id, 1)


async def unlike_comment(
    db: DbSession, item_id: uuid.UUID, comment_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """取消评论点赞（幂等）。未点过赞时直接返回当前计数，不产生负向影响。"""
    await content_port.lock_content_item(db, item_id)
    comment = await _assert_comment_in_item(db, item_id, comment_id)
    repo = ContentCommentLikeRepository(db)
    existing = await repo.get_one_like(comment_id=comment_id, user_id=user_id)
    if existing is None:
        return comment.like_count

    await repo.delete(existing)
    return await bump_like_count(db, comment_id, -1)


async def delete_comment(
    db: DbSession, item_id: uuid.UUID, comment_id: uuid.UUID
) -> None:
    """软删评论并同步递减帖子的 ``comment_count``。

    软删口径与对账的评论明细统计一致（``deleted_at IS NULL``），否则对账下一拍会把减掉的
    一票算回来、与写路径互相覆盖、永久震荡。
    子评论不级联软删：``parent_id`` 保留，回复关系在列表里照旧可见（当前列表是扁平的）。
    鉴权在路由层（``check_owner``：属主放行，代管需 ``content_owner_delete``）。
    """
    comment = await _assert_comment_in_item(db, item_id, comment_id)
    await ContentCommentRepository(db).soft_delete(comment)
    await content_port.bump_comment_count(db, item_id, -1)
