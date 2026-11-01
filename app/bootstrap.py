"""app 侧自注册：把业务模块的模型/任务、错误码、seed 步骤与自有端口实现登记进 core。

装配根 ``boot.assemble`` 调用 :func:`register`。core 因而不知道任何业务模块名，
app 也不需要 import auth。

模型与任务清单来自 :mod:`app.modules.spec`（单一事实源），本模块不再维护第二份字面量。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.spec import APP_MODEL_MODULES, APP_TASK_MODULES

_registered = False


class _ContentStatsImpl:
    """``core.ports.content_stats`` 的业务侧实现（core 不得知道业务表）。"""

    async def count_content_created_by_day(self, days: int) -> dict[str, int]:
        from app.modules.content.daily_stats import count_content_created_by_day

        return await count_content_created_by_day(days)


class _PointsWriterImpl:
    """``core.ports.points`` 的业务侧实现：原样转发，保住同事务与幂等语义。"""

    async def enqueue_event(
        self, db: AsyncSession, user_id: uuid.UUID, event: str, ref_id: str
    ) -> None:
        from app.modules.points.rules import enqueue_points_event

        await enqueue_points_event(db, user_id, event, ref_id)

    async def reward(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        delta: int,
        reason: str,
        ref_type: str,
        ref_id: str,
        *,
        allow_negative: bool = False,
    ) -> Any:
        from app.modules.points.service import reward

        return await reward(
            db,
            user_id,
            delta,
            reason,
            ref_type,
            ref_id,
            allow_negative=allow_negative,
        )

    async def spend(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        amount: int,
        reason: str,
        ref_type: str,
        ref_id: str,
    ) -> Any:
        from app.modules.points.service import spend

        return await spend(db, user_id, amount, reason, ref_type, ref_id)


class _ReportsWriterImpl:
    """``core.ports.reports`` 的实现：转发到 admin 域的写入口径。"""

    async def create_if_absent(
        self,
        db: AsyncSession,
        *,
        type: str,
        target_id: str,
        target_title: str,
        reporter_id: uuid.UUID | None,
        reporter_name: str,
        reason: str,
        created_at: str | None = None,
        explicit_id: uuid.UUID | None = None,
    ) -> bool:
        from app.modules.admin.reports_writer import create_if_absent

        return await create_if_absent(
            db,
            type=type,
            target_id=target_id,
            target_title=target_title,
            reporter_id=reporter_id,
            reporter_name=reporter_name,
            reason=reason,
            created_at=created_at,
            explicit_id=explicit_id,
        )


class _NotificationWriterImpl:
    """``core.ports.notification`` 的实现：转发到 notification 域。"""

    def type_value(self, name: str) -> str:
        from app.modules.notification.service import NotificationType

        return NotificationType[name].value

    async def create(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        type: str,
        payload: dict[str, Any] | None = None,
        actor_id: uuid.UUID | None = None,
        target_id: uuid.UUID | None = None,
    ) -> Any:
        from app.modules.notification.service import create_notification

        return await create_notification(
            db,
            user_id=user_id,
            type=type,
            payload=payload,
            actor_id=actor_id,
            target_id=target_id,
        )


class _ModerationEngineImpl:
    """``core.ports.moderation`` 的实现：转发到 admin 域的规则引擎。"""

    async def load_active_rules(self, db: AsyncSession) -> list[Any]:
        from app.modules.admin.moderation.engine import load_active_rules

        return await load_active_rules(db)

    def evaluate(self, text: str, rules: list[Any]) -> Any:
        from app.modules.admin.moderation.engine import evaluate

        return evaluate(text, rules)


def register() -> None:
    """幂等注册：业务模型/任务、错误码、seed 步骤与自有端口实现。"""
    global _registered
    if _registered:
        return

    from core import ports, task_registry
    from core.db import init_db, model_registry

    for path in APP_MODEL_MODULES:
        model_registry.register_module(path)
    for path in APP_TASK_MODULES:
        task_registry.register_module(path)

    # 各业务模块错误码（导入即 register() 副作用）
    from app.modules import registry as _modules

    _modules.load_errors()

    # schema 就绪后种 RBAC 默认权限（原 core.db.init_db 直连业务 seed，改为登记）
    from app.modules.rbac.seed import seed_rbac

    init_db.register_seed_step(seed_rbac)  # type: ignore[arg-type]

    ports.install("content_stats", _ContentStatsImpl())
    ports.install("reports_writer", _ReportsWriterImpl())
    ports.install("notification_writer", _NotificationWriterImpl())
    ports.install("moderation_engine", _ModerationEngineImpl())
    ports.install("points_writer", _PointsWriterImpl())

    _registered = True
