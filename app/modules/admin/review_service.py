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
from core.ports import content as content_port
from core.ports import files as files_port
from core.ports import projects as projects_port
from core.ports.users import count_admission_users

logger = logging.getLogger(__name__)

async def _pending_reports(db: AsyncSession) -> tuple[int, Any]:
    """举报待审：本域自有模型，直接聚合。"""
    row = (
        await db.execute(
            select(func.count(Report.id), func.min(Report.created_at)).where(
                Report.status == "pending"
            )
        )
    ).one()
    return int(row[0]), row[1]


# (队列 key, 后台深链, 待办聚合)；跨域队列一律经端口，admin 不 import 对方模型。
_QUEUES: tuple[tuple[str, str | None, Any], ...] = (
    ("files", "/admin/files", files_port.pending_files),
    ("reports", "/admin/reports", _pending_reports),
    ("board_applications", None, content_port.pending_board_applications),
    ("column_applications", None, content_port.pending_column_applications),
    ("project_applications", None, projects_port.pending_project_applications),
)


async def build_review_summary(db: AsyncSession) -> AdminReviewSummary:
    """聚合待办队列（只读）；待审账号计数不可用时该队列缺省，不影响其余队列。"""
    now = datetime.now(UTC)
    queues: list[AdminReviewQueueItem] = []
    for key, href, loader in _QUEUES:
        count, oldest = await loader(db)
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
