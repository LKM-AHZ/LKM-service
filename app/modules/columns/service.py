"""专栏（columns）域服务：专栏申请、审核、连载发布与读口。

步骤 9 切片 3 自 ``content.service`` 的「columns 子域」整段迁出，业务语义逐字保留：
申请同一用户仅一条 pending、被拒后 24h 冷却（429 + Retry-After）、审核通过自动建专栏
并 bump 集合版本、列表读缓存按集合版本失效。

专栏连载仍写自己的 ``column_posts`` 表（与 ``content_items`` 的 column_post 并列，
历史「同篇两表」现状未变，见 ``ColumnPost`` 的 docstring）。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.modules.columns.column_models import (
    COLUMN_TABLE_PLAN,
    ColumnApplicationStatus,
    ColumnPostStatus,
)
from app.modules.columns.errors import ColumnErr
from app.modules.columns.models import Column, ColumnApplication, ColumnPost
from app.modules.columns.repository import (
    ColumnApplicationRepository,
    ColumnPostRepository,
    ColumnRepository,
)
from app.modules.columns.schemas import (
    ColumnApplicationCreate,
    ColumnApplicationInfo,
    ColumnApplicationReview,
    ColumnInfo,
    ColumnPostCreate,
    ColumnPostInfo,
)
from core.cache import (
    TTL_ITEM_S,
    TTL_LIST_S,
    bump_collection_version,
    cached_read,
    collection_version,
    make_key,
)
from core.common import PageData, paginate_offset, paginate_pages
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError, ErrCode
from core.metrics import post_created_total
from core.ports.audit import record_audit


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


APPLICATION_COOLDOWN = _dt.timedelta(hours=24)


def _assert_reapply_allowed(
    reviewed_at: _dt.datetime | None,
    errcode: ErrCode,
    now: _dt.datetime,
) -> None:
    """被拒后 24h 冷却：未到期抛 429 并带 ``Retry-After``（秒）。"""
    if reviewed_at is None:
        return
    until = reviewed_at + APPLICATION_COOLDOWN
    if until <= now:
        return
    retry_after = max(1, math.ceil((until - now).total_seconds()))
    hours = max(1, math.ceil(retry_after / 3600))
    raise BizError(
        errcode,
        f"上次申请被拒，约 {hours} 小时后可重新申请",
        headers={"Retry-After": str(retry_after)},
    )


async def create_application(
    db: DbSession, user_id: uuid.UUID, info: ColumnApplicationCreate
) -> ColumnApplicationInfo:
    """提交专栏申请：同一用户仅一条 pending，被拒后 24h 冷却。"""
    repo = ColumnApplicationRepository(db)
    now = now_iso()
    if await repo.has_pending(user_id):
        raise BizError(ColumnErr.APPLICATION_PENDING_EXISTS)
    latest = await repo.latest_rejected(user_id)
    if latest is not None:
        _assert_reapply_allowed(latest.reviewed_at, ColumnErr.APPLICATION_COOLDOWN, now)
    try:
        # 唯一索引兜底并发双提交：只回滚这一条插入，不污染调用方事务。
        async with db.begin_nested():
            app = await repo.create(
                user_id=user_id,
                title=info.title,
                description=info.description,
                reason=info.reason,
            )
    except IntegrityError:
        raise BizError(ColumnErr.APPLICATION_PENDING_EXISTS) from None
    return ColumnApplicationInfo.model_validate(app)


async def list_applications(
    db: DbSession, page: int = 1, limit: int | None = None
) -> PageData[ColumnApplicationInfo]:
    """申请列表，统一返回 ``PageData``。不传 ``limit`` 时返回全部（page 恒为 1，pages 视总数）。"""
    repo = ColumnApplicationRepository(db)
    total = await repo.count()
    apps = await repo.list_page(
        offset=paginate_offset(page, limit) if limit is not None else None,
        limit=limit,
    )
    return PageData(
        items=[ColumnApplicationInfo.model_validate(a) for a in apps],
        total=total,
        page=page,
        pages=paginate_pages(total, limit) if limit else (1 if total else 0),
    )


async def list_my_column_applications(
    db: DbSession, user_id: uuid.UUID
) -> list[ColumnApplicationInfo]:
    """申请人视角：本人提交的专栏申请（状态 / 审核意见 / 提交与审核时间）。"""
    rows = await ColumnApplicationRepository(db).list_for_user(user_id)
    return [ColumnApplicationInfo.model_validate(row) for row in rows]


async def get_application(
    db: DbSession, application_id: uuid.UUID
) -> ColumnApplicationInfo:
    return ColumnApplicationInfo.model_validate(
        await ColumnApplicationRepository(db).get_or_raise(
            application_id, ColumnErr.APPLICATION_NOT_FOUND
        )
    )


async def review_application(
    db: DbSession,
    application_id: uuid.UUID,
    info: ColumnApplicationReview,
    reviewer_id: uuid.UUID,
) -> dict[str, Any]:
    app_repo = ColumnApplicationRepository(db)
    app = await app_repo.get_or_raise(application_id, ColumnErr.APPLICATION_NOT_FOUND)

    # 已审幂等：非 PENDING 状态拒绝再次审批（防状态翻转/重复审批）
    if app.status != ColumnApplicationStatus.PENDING:
        raise BizError(ColumnErr.APPLICATION_ALREADY_REVIEWED)

    app.status = info.status
    app.reviewer_id = reviewer_id
    app.review_note = info.review_note
    app.reviewed_at = now_iso()
    await app_repo.flush()

    column = None
    if info.status == ColumnApplicationStatus.APPROVED:
        column = await _ensure_column_for_application(db, app)

    await record_audit(
        db,
        reviewer_id,
        "review_column_application",
        detail=json.dumps(
            {
                "target_type": "column_application",
                "target_id": str(app.id),
                "applicant_id": str(app.user_id),
                "decision": info.status.value,
                "reason": info.review_note,
                "previous_status": ColumnApplicationStatus.PENDING.value,
                "at": now_iso().isoformat(),
            },
            ensure_ascii=False,
        ),
    )
    return {
        "application": ColumnApplicationInfo.model_validate(app).model_dump(),
        "column": column.model_dump() if column else None,
    }


async def list_columns(
    db: DbSession, page: int = 1, limit: int | None = None
) -> PageData[ColumnInfo]:
    ver = await collection_version("columns")

    async def _load() -> dict[str, Any]:
        repo = ColumnRepository(db)
        total = await repo.count()
        cols = await repo.list_page(
            offset=paginate_offset(page, limit) if limit is not None else None,
            limit=limit,
        )
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


async def get_column(db: DbSession, column_id: uuid.UUID) -> ColumnInfo:
    async def _load() -> dict[str, Any]:
        return ColumnInfo.model_validate(
            await ColumnRepository(db).get_or_raise(column_id, ColumnErr.NOT_FOUND)
        ).model_dump()

    payload = await cached_read(make_key("columns:by_id", column_id), TTL_ITEM_S, _load)
    return ColumnInfo.model_validate(payload)


async def get_column_by_slug(db: DbSession, slug: str) -> ColumnInfo:
    async def _load() -> dict[str, Any]:
        return ColumnInfo.model_validate(
            await ColumnRepository(db).get_one_or_raise(
                ColumnErr.NOT_FOUND, Column.slug == slug
            )
        ).model_dump()

    payload = await cached_read(make_key("columns:by_slug", slug), TTL_ITEM_S, _load)
    return ColumnInfo.model_validate(payload)


async def create_post(
    db: DbSession, column_id: uuid.UUID, info: ColumnPostCreate, author_id: uuid.UUID
) -> ColumnPostInfo:
    await ColumnRepository(db).get_or_raise(column_id, ColumnErr.NOT_FOUND)

    post = await ColumnPostRepository(db).create(
        column_id=column_id,
        author_id=author_id,
        title=info.title,
        summary=info.summary,
        content=info.content,
        status=ColumnPostStatus.PUBLISHED,
        published_at=now_iso(),
    )
    post_created_total.labels("column_post_native").inc()
    return ColumnPostInfo.model_validate(post)


async def list_posts(
    db: DbSession, column_id: uuid.UUID, page: int = 1, limit: int | None = None
) -> PageData[ColumnPostInfo]:
    await ColumnRepository(db).get_or_raise(column_id, ColumnErr.NOT_FOUND)
    repo = ColumnPostRepository(db)
    total = await repo.count(ColumnPost.column_id == column_id)
    posts = await repo.list_in_column(
        column_id,
        offset=paginate_offset(page, limit) if limit is not None else None,
        limit=limit,
    )
    return PageData(
        items=[ColumnPostInfo.model_validate(p) for p in posts],
        total=total,
        page=page,
        pages=paginate_pages(total, limit) if limit else (1 if total else 0),
    )


async def get_post(
    db: DbSession, post_id: uuid.UUID, column_id: uuid.UUID | None = None
) -> ColumnPostInfo:
    filters = [ColumnPost.id == post_id]
    if column_id is not None:
        filters.append(ColumnPost.column_id == column_id)
    return ColumnPostInfo.model_validate(
        await ColumnPostRepository(db).get_one_or_raise(
            ColumnErr.POST_NOT_FOUND, *filters
        )
    )


async def _ensure_column_for_application(
    db: DbSession, application: ColumnApplication
) -> ColumnInfo:
    repo = ColumnRepository(db)
    col = await repo.get_by_application(application.id)
    if col:
        return ColumnInfo.model_validate(col)

    col = await repo.create(
        owner_id=application.user_id,
        application_id=application.id,
        title=application.title,
        description=application.description,
    )
    # 集合新增：升级列列表版本号，使旧分页缓存立即失效（写后读一致）
    await bump_collection_version("columns")
    return ColumnInfo.model_validate(col)
