"""每日待办摘要：把 ``review-summary`` 的聚合结果发给所有管理员（站内信）。

载体选择站内信（落库为准）+ 现有通知推送通道，不引入新客户端：
- 收件人 = auth 权威侧 ``account_level=admin`` 的用户（业务库无 users，经 seam 取）。
- 无待办时**不发**（避免每天一条“0 条待办”的噪音）。
- 系统通知（actor/target 均为 None），同一人多天各一条，不聚合。
- 文案带各队列数量与 dashboard 深链；取不到管理员名单时 fail-closed 放弃本次发送。
"""

from __future__ import annotations

import logging

from app.modules.admin.review_service import build_review_summary, queue_labels
from app.modules.notification.service import NotificationType, create_notification
from core.db.session import new_worker_session as new_session
from core.ports.users import list_admin_ids

logger = logging.getLogger(__name__)

DASHBOARD_URL = "/admin"


async def send_daily_digest() -> int:
    """给每个管理员落一条待办摘要，返回通知条数（0 = 无待办/无收件人）。"""
    db = await new_session()
    try:
        summary = await build_review_summary(db)
        if summary.total_pending == 0:
            return 0

        admin_ids = await list_admin_ids(db)
        if not admin_ids:
            logger.warning("review digest: 无 admin 收件人，跳过本次发送")
            return 0

        labels = queue_labels()
        parts = [
            f"{labels.get(item.key, item.key)} {item.count}"
            for item in summary.queues
            if item.count
        ]
        title = f"待办审核 {summary.total_pending} 条：" + "，".join(parts)
        payload = {
            "title": title,
            "url": DASHBOARD_URL,
            "total_pending": summary.total_pending,
            "counts": {item.key: item.count for item in summary.queues},
        }

        for admin_id in admin_ids:
            await create_notification(
                db,
                user_id=admin_id,
                type=NotificationType.REVIEW_DIGEST.value,
                payload=payload,
            )
        await db.commit()
        logger.info("review digest 已发送 admin=%d", len(admin_ids))
        return len(admin_ids)
    except Exception:
        await db.rollback()
        logger.exception("review digest 发送失败")
        raise
    finally:
        await db.close()
