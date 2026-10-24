"""后台待办审核聚合（service 层）：dashboard 卡片、侧边栏角标与每日摘要共用同一口径。

队列：文件待审、举报待处理、板块/专栏/项目申请待审，外加 auth 域的待审账号。
业务表在业务库内聚合；待审账号经 users 端口取 auth 权威计数（不可用时该队列缺省、
不影响其余队列与 dashboard 可用性）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Report
from app.modules.admin.schemas import AdminReviewQueueItem, AdminReviewSummary
from app.modules.content.models import BoardApplication, ColumnApplication
from app.modules.files.models import FileStatus, LibraryFile
from app.modules.projects.models import ProjectApplication
from core.ports.users import count_admission_users

logger = logging.getLogger(__name__)

# (key, model, status 列, 待办状态, 后台深链)；暂无审核页的队列 href=None。
_QUEUES: tuple[tuple[str, Any, Any, str, str | None], ...] = (
    (
        "files",
        LibraryFile,
        LibraryFile.status,
        FileStatus.PENDING.value,
        "/admin/files",
    ),
    ("reports", Report, Report.status, "pending", "/admin/reports"),
    (
        "board_applications",
        BoardApplication,
        BoardApplication.status,
        "pending",
        None,
    ),
    (
        "column_applications",
        ColumnApplication,
        ColumnApplication.status,
        "pending",
        None,
    ),
    (
        "project_applications",
        ProjectApplication,
        ProjectApplication.status,
        "pending",
        None,
    ),
)


async def build_review_summary(db: AsyncSession) -> AdminReviewSummary:
    """聚合待办队列（只读）；待审账号计数不可用时该队列缺省，不影响其余队列。"""
    now = datetime.now(UTC)
    queues: list[AdminReviewQueueItem] = []
    for key, model, status_col, pending, href in _QUEUES:
        count, oldest = (
            await db.execute(
                select(func.count(model.id), func.min(model.created_at)).where(
                    status_col == pending
                )
            )
        ).one()
        waiting = (
            max(0, int((now - oldest).total_seconds())) if oldest is not None else None
        )
        queues.append(
            AdminReviewQueueItem(
                key=key,
                count=int(count),
                oldest_waiting_seconds=waiting,
                href=href,
            )
        )

    # auth 域队列：计数走 users 端口（拆库 HTTP / 单库直查）；失败只丢这一项。
    try:
        pending_users = await count_admission_users(db, "pending")
    except Exception:
        logger.warning("review summary: admission count unavailable", exc_info=True)
    else:
        queues.append(
            AdminReviewQueueItem(
                key="admission_users",
                count=pending_users,
                oldest_waiting_seconds=None,
                href="/admin/users",
            )
        )

    return AdminReviewSummary(
        total_pending=sum(item.count for item in queues),
        queues=queues,
    )


def queue_labels() -> dict[str, str]:
    """队列 key → 中文标签（每日摘要文案用；dashboard 走前端 i18n）。"""
    return {
        "files": "文件待审",
        "reports": "举报待处理",
        "board_applications": "板块申请",
        "column_applications": "专栏申请",
        "project_applications": "项目申请",
    }
