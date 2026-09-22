"""Onboarding 引导向导进度服务 —— 每用户一行的分步持久化。

与前端 ``useOnboardingFlow`` 的 ``OnboardingState`` 契约对齐：
``data`` 为以步骤号为 key 的分步合并数据（如 ``{1: {...}, 2: {...}}``）。
"""

import uuid
from typing import Any

from app.db.repository import DbSession
from auth.models import OnboardingProgress
from auth.repository import OnboardingProgressRepository
from auth.schemas import OnboardingState

#: 向导最后一步（跳过时要把 step 置到这里）。事实源应与 router_onboarding.ONBOARDING_STEPS
#: 一致——服务层不能 import router（router 已 import 本模块，会成环），故常量先落在本模块，
#: 待 router 侧改为 `from auth.service_onboarding import ONBOARDING_LAST_STEP` 后即单一来源。
ONBOARDING_LAST_STEP = 4


async def get_or_create_progress(
    db: DbSession, user_id: uuid.UUID
) -> OnboardingProgress:
    """返回某用户的引导进度；未开始时新建一条默认记录并返回。

    「先查后建」不是原子的：读端点（GET /auth/onboarding）与写端点（PUT .../steps/N）并发、
    或前端 strict-mode 双跑时，两个请求会同时看到 None 各自 INSERT；主键冲突的一方被
    get_auth_session 转成 ALREADY_REGISTERED（读端点上一个误导性的 4xx，且连带回滚本次要提交
    的步骤数据）。故改用 INSERT ... ON CONFLICT DO NOTHING 再重读：并发下必有一方读到对方
    已提交的行（READ COMMITTED 下 ON CONFLICT 会等对方提交后再判定）。
    """
    repo = OnboardingProgressRepository(db)
    row = await repo.get_by_user(user_id)
    if row is None:
        await repo.pg_upsert(
            {"user_id": user_id}, index_elements=["user_id"], do_nothing=True
        )
        row = await repo.get_by_user(user_id)
        if row is None:  # 理论上不可达：刚插入或并发方已提交
            raise RuntimeError(f"onboarding progress row unavailable user_id={user_id}")
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
    row.step = ONBOARDING_LAST_STEP
    await OnboardingProgressRepository(db).flush()
    return _to_state(row)
