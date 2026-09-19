"""Onboarding 引导向导进度服务 —— 每用户一行的分步持久化。

与前端 ``useOnboardingFlow`` 的 ``OnboardingState`` 契约对齐：
``data`` 为以步骤号为 key 的分步合并数据（如 ``{1: {...}, 2: {...}}``）。
"""

import uuid
from typing import Any

from app.db.repository import DbSession
from app.modules.auth.models import OnboardingProgress
from app.modules.auth.repository import OnboardingProgressRepository
from app.modules.auth.schemas import OnboardingState


async def get_or_create_progress(
    db: DbSession, user_id: uuid.UUID
) -> OnboardingProgress:
    """返回某用户的引导进度；未开始时新建一条默认记录并返回。"""
    repo = OnboardingProgressRepository(db)
    row = await repo.get_by_user(user_id)
    if row is None:
        row = await repo.create(user_id=user_id)
    return row


def _to_state(row: OnboardingProgress) -> OnboardingState:
    return OnboardingState(
        step=row.step,
        completed=row.completed,
        data=row.data or None,
    )


async def get_onboarding_state(db: DbSession, user_id: uuid.UUID) -> OnboardingState:
    """读取引导进度：未开始返回默认 step=1，不 404。"""
    row = await get_or_create_progress(db, user_id)
    return _to_state(row)


async def set_onboarding_step(
    db: DbSession, user_id: uuid.UUID, step: int, data: dict[str, Any]
) -> OnboardingState:
    """提交某一步的分步数据：合并进整体 data、更新当前 step。"""
    row = await get_or_create_progress(db, user_id)
    merged: dict[str, Any] = dict(row.data or {})
    merged[str(step)] = data
    row.data = merged
    row.step = step
    await OnboardingProgressRepository(db).flush()
    return _to_state(row)


async def mark_onboarding_skipped(db: DbSession, user_id: uuid.UUID) -> OnboardingState:
    """整体跳过引导并视为完成。"""
    row = await get_or_create_progress(db, user_id)
    row.completed = True
    row.step = 4
    await OnboardingProgressRepository(db).flush()
    return _to_state(row)
