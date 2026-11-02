import datetime as _dt
import json
import math
import re
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

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
from app.modules.content.counters import (
    bump_content_counter,
    read_count,
)
from app.modules.content.errors import ContentErr
from app.modules.content.events import (
    CONTENT_ACTION_DELETED,
    enqueue_content_event,
)
from app.modules.content.models import (
    Board,
    BoardApplication,
    ContentItem,
    ContentStatus,
    ContentType,
)
from app.modules.content.repository import (
    BoardApplicationRepository,
    BoardBanRepository,
    BoardRepository,
    ContentItemRepository,
    ContentLikeRepository,
)
from app.modules.content.schemas import (
    ContentItemCreate,
    ContentItemInfo,
)
from core.cache import (
    bump_collection_version,
    cache_invalidate,
    cached_read,
    collection_version,
    make_key,
)
from core.common import PageData, paginate_offset, paginate_pages
from core.config import settings
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError, ErrCode
from core.metrics import post_created_total
from core.ports import columns as columns_port
from core.ports import comment as comment_port
from core.ports import feed as feed_port
from core.ports import points as points_port
from core.ports import reports as reports_port
from core.ports.audit import record_audit
from core.ports.snapshot import get_user_display_names as _author_map
from core.ports.snapshot import get_user_snapshot

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


async def _column_title_map(
    db: DbSession, column_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """内容项展示用的专栏标题（专栏已拆到 columns 域，经端口批量取）。"""
    if not column_ids:
        return {}
    return await columns_port.column_titles(db, set(column_ids))


async def list_items(
    db: DbSession,
    page: int = 1,
    limit: int = 20,
    board_id: uuid.UUID | None = None,
    content_type: str | None = None,
    author_id: uuid.UUID | None = None,
) -> PageData[ContentItemInfo]:
    repo = ContentItemRepository(db)

    # 计数走短 TTL 缓存 + content 集合版本号：任何增删 ContentItem 的行都会
    # bump_content_version() 递增集合版本，使旧(count)键立即失效，杜绝脏计数。
    async def _count() -> int:
        return await repo.count_published(
            board_id=board_id, content_type=content_type, author_id=author_id
        )

    ver = await collection_version("content")
    total_count = await cached_read(
        make_key(
            "content:list:total",
            ver,
            board_id or "",
            content_type or "",
            author_id or "",
        ),
        60,
        _count,
    )

    items = await repo.list_published(
        board_id=board_id,
        content_type=content_type,
        author_id=author_id,
        offset=paginate_offset(page, limit),
        limit=limit,
    )

    author_ids = [i.author_id for i in items if i.author_id]
    column_ids = [i.column_id for i in items if i.column_id]
    names = await _author_map(db, author_ids)
    cols = await _column_title_map(db, column_ids)
    out = [
        _item_to_schema(
            i,
            names.get(i.author_id, "") if i.author_id else (i.publisher or ""),
            cols.get(i.column_id) if i.column_id else None,
        )
        for i in items
    ]
    return PageData(
        items=out,
        total=total_count,
        page=page,
        pages=paginate_pages(total_count, limit),
    )


async def _new_write_session() -> DbSession:
    from core.db.session import new_session

    return await new_session()


async def bump_item_view(item_id: uuid.UUID) -> None:
    """原子地给内容项 view_count +1（供公开详情阅读计数增长路径）。

    前端详情走 GraphQL ``contentItem``，此前 get_item 恒 bump_view=False 且 REST 无详情
    端点，导致 view_count 无增长路径。GraphQL 用只读会话（读后显式 rollback），不能在其上写，
    故自建独立写会话原子 UPDATE 并 commit，原子 UPDATE 避免并发 read-modify-write 丢计数。
    """
    dbw = await _new_write_session()
    try:
        await ContentItemRepository(dbw).bump_view_count(item_id)
        await dbw.commit()
    finally:
        await dbw.close()


# ── 帖详情缓存（蓝图 §5.6 `cache:content:{id}`）─────────────────────────────
# 缓存**只含稳定字段**的详情快照，5 个高频互动计数刻意排除：它们的变更不经 content.*
# 事件（走 counters 写穿 / 浏览上报），若一并缓存，一次点赞/评论后详情页数字会在 TTL 内
# 「跳回去」——陈旧窗口被放大到整个 TTL（默认 60s，蓝图口径「秒级短 TTL」实为 60s）。
# 计数改由缓存命中后一次**窄列** SELECT 叠加实时值（正文是大 TOAST 列，窄列读不触
# detoast，缓存真正省下的是正文传输 + 作者快照 + 栏目名查询）。
_DETAIL_VOLATILE_COUNTERS: tuple[str, ...] = (
    "view_count",
    "like_count",
    "comment_count",
    "bookmark_count",
    "forward_count",
)


def _detail_cache_key(item_id: uuid.UUID) -> str:
    """帖详情缓存键。读/失效两侧共用本函数，防键前缀漂移导致「写了不失效」。"""
    return make_key("content:item", item_id)


def _detail_snapshot(
    item: ContentItem, author_name: str, column_title: str | None
) -> dict[str, Any]:
    """可 JSON 化的详情快照（cache_set 走 json.dumps，故需 JSON 原生类型），**不含互动计数**。"""
    data = _item_to_schema(item, author_name, column_title).model_dump(mode="json")
    for field in _DETAIL_VOLATILE_COUNTERS:
        data.pop(field, None)
    return data


async def clear_item_detail_cache(item_id: uuid.UUID) -> None:
    """失效某条内容的详情缓存（内容变更/删除后调用；Redis 不可用时静默跳过）。

    ``cache:content:{id}`` 的失效口径是「事件刷新」——内容本体/状态变更或软删时删除该键；
    **计数变更不失效**（计数本就不入缓存，见上方说明），避免高频点赞把详情缓存打穿。
    """
    await cache_invalidate(_detail_cache_key(item_id))


async def _item_detail_context(
    db: DbSession, item: ContentItem
) -> tuple[str, str | None]:
    """详情展示所需的作者名与栏目名（get_item/get_item_by_slug 的公共取数）。"""
    if item.author_id:
        names = await _author_map(db, [item.author_id])
        author_name = names.get(item.author_id, "")
    else:
        author_name = item.publisher or ""
    column_title = ""
    if item.column_id:
        cols = await _column_title_map(db, [item.column_id])
        column_title = cols.get(item.column_id, "")
    return author_name, column_title or None


async def get_item(
    db: DbSession, item_id: uuid.UUID, bump_view: bool = False
) -> ContentItemInfo:
    repo = ContentItemRepository(db)
    if bump_view:
        await repo.bump_view_count(item_id)

    async def _load() -> dict[str, Any]:
        item = await repo.get_or_raise(item_id, ContentErr.CONTENT_NOT_FOUND)
        author_name, column_title = await _item_detail_context(db, item)
        return _detail_snapshot(item, author_name, column_title)

    payload = await cached_read(
        _detail_cache_key(item_id),
        settings.content_detail_cache_ttl_s,
        _load,
    )
    info = ContentItemInfo.model_validate(payload)
    # 命中缓存也叠加实时计数：计数不入缓存（见上方说明），每次读取一次窄列值。
    counters = await repo.read_counter_snapshot(item_id)
    if counters is None:
        # 缓存命中但行已删（删除路径会失效缓存，此处兜并发窗口）：按「不存在」处理
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    return info.model_copy(
        update=dict(zip(_DETAIL_VOLATILE_COUNTERS, counters, strict=True))
    )


async def get_item_by_slug(db: DbSession, slug: str) -> ContentItemInfo:
    """按 slug 读详情：先窄列解析出 id，再复用 :func:`get_item` 的同一份缓存。

    与 get_item 共用一个键，避免「同一条内容按 id/按 slug 打开各缓存一份、失效要删两处」。
    """
    item_id = await ContentItemRepository(db).id_by_slug(slug)
    if item_id is None:
        raise BizError(ContentErr.CONTENT_NOT_FOUND)
    return await get_item(db, item_id)


async def _require_unique_slug(db: DbSession, slug: str | None) -> None:
    if not slug:
        return
    # 应用层唯一校验，返回领域错误码 SLUG_TAKEN（DB 侧唯一索引兜底）；
    # 含已软删行（墓碑占位），防删除后同 slug 复活歧义。
    if await ContentItemRepository(db).slug_taken(slug):
        raise BizError(ContentErr.SLUG_TAKEN)


async def create_item(
    db: DbSession,
    author_id: uuid.UUID,
    info: ContentItemCreate,
) -> ContentItemInfo:
    # 未知体裁不能绕过下方按体裁执行的发言准入校验。
    try:
        ContentType(info.content_type)
    except ValueError:
        raise BizError(ContentErr.UNSUPPORTED_TYPE) from None

    # 发言准入：板块存在 / 认证 / 日限发（仅社区用户写作体裁）
    if info.content_type in (
        ContentType.DISCUSSION,
        ContentType.COLUMN_POST,
        ContentType.BLOG_POST,
        ContentType.QA,
    ):
        from app.modules.content.boards.service import check_post_allowed

        await check_post_allowed(db, info.board_id, author_id)

    await BoardRepository(db).get_or_raise(info.board_id, ContentErr.BOARD_NOT_FOUND)

    # 专栏已拆到 columns 域：存在性经端口判定，错误码仍用 content 自己的 17.4
    if info.content_type == ContentType.COLUMN_POST and (
        not info.column_id
        or not await columns_port.column_exists(db, info.column_id)
    ):
        raise BizError(ContentErr.COLUMN_NOT_FOUND)

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
    await ContentItemRepository(db).add(item)
    # 索引失效通知（与业务同事务；草稿/待审态不发，见 content/events.py）
    await enqueue_content_event(db, item)

    # 积分事件（异步计分，不阻塞 200）
    await points_port.enqueue_event(db, author_id, "post", f"item:{item.id}")
    await bump_collection_version("content")

    # M0.5.2 业务指标：本方法统一写 content_items（discussion/column_post/
    # article/blog_post/qa），谓成功落库即全内容 dumps 的“发帖/新版产出”。
    post_created_total.labels(item.content_type).inc()

    names = await _author_map(db, [author_id])
    return _item_to_schema(item, names.get(author_id, ""))


async def delete_item(
    db: DbSession,
    item_id: uuid.UUID,
    current_user_id: uuid.UUID,
    as_admin: bool = False,
) -> uuid.UUID:
    """软删统一内容项并返回作者 id（admin 代删时可能无作者，返回 nil UUID）。

    批 4：改软删（``deleted_at`` 置位），行保留以便恢复；可见性由各查询面的
    Repository 基类过滤统一收口。物化 feed 是内容快照，不会自动跟随，故一并按
    ``source_id`` 清掉该条目的物化行，否则关注者时间线仍能看到已删内容。
    """
    repo = ContentItemRepository(db)
    item = await repo.get_or_raise(item_id, ContentErr.CONTENT_NOT_FOUND)
    author_id = item.author_id or uuid.UUID(int=0)
    await repo.soft_delete(item)
    # 帖详情缓存失效（§5.6「事件刷新」）：软删后详情不得再从缓存命中旧快照
    await clear_item_detail_cache(item_id)
    await enqueue_content_event(db, item, CONTENT_ACTION_DELETED)
    await feed_port.remove_source_item(db, item_id)
    await bump_collection_version("content")
    return author_id


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


async def like_item(db: DbSession, item_id: uuid.UUID, user_id: uuid.UUID) -> int:
    # 与明细查/写及计数更新保持同一把行锁，重复请求不会同时通过存在性检查。
    await ContentItemRepository(db).lock_active(item_id)
    existing = await ContentLikeRepository(db).get_one_like(
        content_id=item_id, user_id=user_id
    )
    if existing is not None:
        return await read_count(db, item_id, "like_count")

    await ContentLikeRepository(db).create(content_id=item_id, user_id=user_id)
    await points_port.enqueue_event(db, user_id, "like", f"item:{item_id}")
    return await bump_content_counter(db, item_id, "like_count", 1)


async def unlike_item(db: DbSession, item_id: uuid.UUID, user_id: uuid.UUID) -> int:
    await ContentItemRepository(db).lock_active(item_id)
    existing = await ContentLikeRepository(db).get_one_like(
        content_id=item_id, user_id=user_id
    )
    if existing is None:
        return await read_count(db, item_id, "like_count")
    await ContentLikeRepository(db).delete(existing)
    return await bump_content_counter(db, item_id, "like_count", -1)


async def report_content(
    db: DbSession,
    user_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    reason: str,
) -> None:
    """提交内容举报，落 admin 域的 ``reports`` 表（后台「举报」页据此审核）。

    - ``target_title`` 由后端从内容行回填，**不接受前端传入**：审核队列要显示的是内容自己的
      标题/正文摘录，客户端能随意伪造的话队列里只剩无意义字符串。
    - 同一用户对同一目标的**待处理**举报只留一条：重复提交静默成功（不报错、也不重复入队），
      否则一个人连点就能把队列刷满。
    - 是否允许举报（只要求登录）由路由层决定，见 ``router.py`` 的说明。
    """
    if target_type == "post":
        item = await ContentItemRepository(db).get_or_raise(
            target_id, ContentErr.CONTENT_NOT_FOUND
        )
        title = item.title
    elif target_type == "comment":
        # 评论已拆到 comment 域：正文经端口取，不存在由属主域抛 COMMENT_NOT_FOUND
        title = await comment_port.comment_body(db, target_id)
    else:
        raise BizError(ContentErr.UNSUPPORTED_TYPE)

    names = await _author_map(db, [user_id])
    await reports_port.create_if_absent(
        db,
        type=target_type,
        target_id=str(target_id),
        target_title=title[:200],
        reporter_id=user_id,
        reporter_name=names.get(user_id, ""),
        reason=reason[:500],
    )


async def forward_item(db: DbSession, item_id: uuid.UUID) -> int:
    """记一次转发（用户把内容链接带出去），返回新的 ``forward_count``。

    与「浏览上报」相对：这里的动作是用户主动复制/分享链接，由前端在复制成功后才上报，
    故不需要幂等去重——同一人分享多次就是多次；也**没有**明细表可对账
    （见 ``counters.py`` 中与 ``view_count`` 并列的说明）。
    """
    return await ContentItemRepository(db).bump_forward_count(item_id)


# ---- 供 blog publish 等内部调用（直接落 content_items）----


async def publish_blog_item(
    db: DbSession,
    owner_id: uuid.UUID,
    *,
    board_id: uuid.UUID,
    slug: str | None,
    title: str,
    content: str,
    summary: str | None,
    cover: str | None,
    tags: list[str],
) -> uuid.UUID:
    """把 blog 发布产物落成一条 content_items（content_type=blog_post）。

    幂等：同 slug 更新现有行（重发），否则新建。返回 content_items.id。
    """
    repo = ContentItemRepository(db)
    existing = await repo.get_by_slug(slug) if slug else None

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
        await repo.flush()
        await clear_item_detail_cache(existing.id)
        await enqueue_content_event(db, existing)
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
    await repo.add(item)
    await enqueue_content_event(db, item)
    # M0.5.2：仅“新建”分支记入博客发布产出；同 slug 重发（上面 existing 更新早退）不复计。
    post_created_total.labels(ContentType.BLOG_POST).inc()
    await points_port.enqueue_event(db, owner_id, "post", f"item:{item.id}")
    await bump_collection_version("content")
    return item.id


# =====================================================================
# boards(版块) 子域 —— 归入 content 单一 service 命名空间。
# 原实现迁自 app/modules/content/boards/service.py；boards/service.py 现为薄 re-export 桩。
# 表(Board/BoardApplication/BoardBan)与 schemas/errors 保持原样；发言准入
# ``check_post_allowed`` 经 content/service.create_item 懒 import 引用（供测试按
# boards.service 路径 monkeypatch），语义不变。
# =====================================================================


def _assert_reapply_allowed(
    reviewed_at: _dt.datetime | None,
    errcode: ErrCode,
    now: _dt.datetime,
) -> None:
    """被拒后 24h 冷却：未到期抛 429 并带 ``Retry-After``（秒）。"""
    if reviewed_at is None:
        return
    until = reviewed_at + BOARD_APPLICATION_COOLDOWN
    if until <= now:
        return
    retry_after = max(1, math.ceil((until - now).total_seconds()))
    hours = max(1, math.ceil(retry_after / 3600))
    raise BizError(
        errcode,
        f"上次申请被拒，约 {hours} 小时后可重新申请",
        headers={"Retry-After": str(retry_after)},
    )


BOARD_APPLICATION_COOLDOWN = _dt.timedelta(hours=24)


def _board_to_schema(b: Board) -> BoardOut:
    return BoardOut.model_validate(b)


def _bb_application_to_schema(a: BoardApplication) -> BoardApplicationOut:
    return BoardApplicationOut.model_validate(a)


# ————— Board CRUD —————
async def create_board_ex(
    db: DbSession, info: BoardCreate, owner_id: uuid.UUID | None
) -> BoardOut:
    repo = BoardRepository(db)
    if await repo.slug_taken(info.slug):
        raise BizError(BoardErr.SLUG_CONFLICT)
    board = await repo.create(
        slug=info.slug,
        title=info.title,
        description=info.description,
        owner_id=owner_id,
        parent_id=info.parent_id,
        require_certified=info.require_certified,
        daily_post_limit=info.daily_post_limit,
        is_public=info.is_public,
    )
    return _board_to_schema(board)


async def list_boards(db: DbSession) -> list[BoardOut]:
    rows = await BoardRepository(db).list_ordered()
    return [_board_to_schema(b) for b in rows]


async def get_board_ex(db: DbSession, board_id: uuid.UUID) -> Board:
    return await BoardRepository(db).get_or_raise(board_id, BoardErr.BOARD_NOT_FOUND)


async def update_board_ex(
    db: DbSession,
    board_id: uuid.UUID,
    owner_id: uuid.UUID,
    patch: BoardUpdate,
    *,
    is_admin: bool = False,
) -> BoardOut:
    """更新板块。``patch.version`` 可选（蓝图 §6.1 乐观锁）：传了走 CAS，不符抛
    :class:`VersionConflictError`（端点转 409 + 当前值）；不传跳过校验但仍递增版本。"""
    board = await get_board_ex(db, board_id)
    _assert_owner(board, owner_id, is_admin)
    data = patch.model_dump(exclude_unset=True)
    expected_version = data.pop("version", None)
    repo = BoardRepository(db)
    if expected_version is not None or data:
        await repo.update_cas(board, expected_version, **data)
    else:
        await repo.flush()
    return _board_to_schema(board)


def _assert_owner(
    board: Board, current_user_id: uuid.UUID, is_admin: bool = False
) -> None:
    # 防御性断言：非属主且非 admin(代管) → 拒。路由层 check_owner 已先做对象级
    # 判定(board_owner_manage)，此处 is_admin 由路由的会话权限裁决结果传入。
    if board.owner_id != current_user_id and not is_admin:
        raise BizError(BoardErr.NOT_BOARD_OWNER)


# ————— 板块申请/审核 —————
async def submit_application(
    db: DbSession, applicant_id: uuid.UUID, info: BoardApplicationCreate
) -> BoardApplicationOut:
    """提交板块申请：同一 slug 同一条待审，被拒后释放 slug 并冷却 24h。"""
    repo = BoardApplicationRepository(db)
    if await repo.has_pending(applicant_id, info.slug):
        raise BizError(BoardErr.APPLICATION_PENDING_EXISTS)
    if await repo.slug_taken(info.slug):
        raise BizError(BoardErr.SLUG_CONFLICT)
    if await BoardRepository(db).slug_taken(info.slug):
        raise BizError(BoardErr.SLUG_CONFLICT)
    latest = await repo.latest_rejected(applicant_id, info.slug)
    if latest is not None:
        _assert_reapply_allowed(
            latest.reviewed_at, BoardErr.APPLICATION_COOLDOWN, now_iso()
        )
    try:
        async with db.begin_nested():
            app_ = await repo.create(
                applicant_id=applicant_id,
                title=info.title,
                description=info.description,
                reason=info.reason,
                slug=info.slug,
                status="pending",
            )
    except IntegrityError:
        # 并发下撞 uq_board_applications_slug_active：slug 仍被非被拒申请占用
        raise BizError(BoardErr.SLUG_CONFLICT) from None
    return _bb_application_to_schema(app_)


async def list_my_board_applications(
    db: DbSession, applicant_id: uuid.UUID
) -> list[BoardApplicationOut]:
    """申请人视角：本人提交的板块申请（状态 / 审核意见 / 提交与审核时间）。"""
    rows = await BoardApplicationRepository(db).list_for_applicant(applicant_id)
    return [_bb_application_to_schema(row) for row in rows]


async def review_board_application(
    db: DbSession,
    application_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    body: ReviewBoardApplicationRequest,
) -> BoardApplicationOut:
    repo = BoardApplicationRepository(db)
    app_ = await repo.get_or_raise(application_id, BoardErr.APPLICATION_NOT_FOUND)
    if app_.status != "pending":
        raise BizError(BoardErr.APPLICATION_ALREADY_REVIEWED)
    if body.approve and await BoardRepository(db).slug_taken(app_.slug):
        raise BizError(BoardErr.SLUG_CONFLICT)
    app_.status = "approved" if body.approve else "rejected"
    app_.reviewer_id = reviewer_id
    app_.review_note = body.note
    app_.reviewed_at = now_iso()
    await repo.flush()
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
    await record_audit(
        db,
        reviewer_id,
        "review_board_application",
        detail=json.dumps(
            {
                "target_type": "board_application",
                "target_id": str(app_.id),
                "applicant_id": str(app_.applicant_id),
                "decision": app_.status,
                "reason": body.note,
                "previous_status": "pending",
                "at": now_iso().isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    return _bb_application_to_schema(app_)


# ————— 禁言 —————
async def ban_user(
    db: DbSession,
    board: Board,
    actor_id: uuid.UUID,
    body: BanRequest,
    *,
    is_admin: bool = False,
) -> None:
    _assert_owner(board, actor_id, is_admin)
    repo = BoardBanRepository(db)
    already = await repo.get_active(
        board_id=board.id, user_id=body.user_id, now=now_iso()
    )
    if already is not None:
        raise BizError(BoardErr.ALREADY_BANNED)
    await repo.create(
        board_id=board.id,
        user_id=body.user_id,
        created_by=actor_id,
        reason=body.reason,
        expires_at=now_iso() + _dt.timedelta(hours=body.hours),
    )


async def unban_user(
    db: DbSession,
    board: Board,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
    *,
    is_admin: bool = False,
) -> None:
    _assert_owner(board, actor_id, is_admin)
    await BoardBanRepository(db).hard_delete_for(
        board_id=board.id, user_id=target_user_id
    )


async def is_banned(db: DbSession, board_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    active = await BoardBanRepository(db).get_active(
        board_id=board_id, user_id=user_id, now=now_iso()
    )
    return active is not None


# ————— 发言准入（供 forum create_post 调用）—————
async def check_post_allowed(
    db: DbSession, board_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """校验用户在板块的发帖资格：板块存在 / 可见 / 未禁言 / 认证 / 日限发。异常抛相应 BoardErr。"""
    board = await get_board_ex(db, board_id)
    if board.status != "active":
        raise BizError(BoardErr.BOARD_INACTIVE)
    if not board.is_public:
        # 私有板块：需 normal 以上（认证成员）。account_level 判给 auth 快照缝（含 banned 态）。
        _snap = await get_user_snapshot(db, user_id=user_id)
        ulevel = _snap.account_level if _snap else None
        if ulevel not in ("normal", "admin"):
            raise BizError(BoardErr.BOARD_NOT_PUBLIC)
    if await is_banned(db, board.id, user_id):
        raise BizError(BoardErr.BOARD_BANNED)
    # 初级通识考试通过判定：证书来自 type=exam 且 unlock_level=normal 的认证考试
    if board.require_certified and not await BoardRepository(
        db
    ).has_normal_certification(user_id):
        raise BizError(BoardErr.CERTIFICATION_REQUIRED)
    if board.daily_post_limit > 0:
        today_start = now_iso().replace(hour=0, minute=0, second=0, microsecond=0)
        cnt = await ContentItemRepository(db).count_daily_discussions(
            author_id=user_id, board_id=board_id, since=today_start
        )
        if cnt >= board.daily_post_limit:
            raise BizError(BoardErr.DAILY_POST_LIMIT_REACHED)
