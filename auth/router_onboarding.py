"""Onboarding 路由 —— 注册后四步引导向导的分步持久化。

所有端点都需要已登录用户（get_current_user）。

GET  /auth/onboarding                  -> 当前引导进度（未开始返回默认 step=1）
PUT  /auth/onboarding/steps/{step}     {data} -> 合并某一步分步数据
POST /auth/onboarding/skip             -> 整体跳过并视为完成
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common import ApiResp
from app.core.err import BizError, CommonErr, respond
from auth.db.session import get_auth_session
from auth.deps import CurrentUser, get_current_user
from auth.schemas import OnboardingState, OnboardingStepRequest
from auth.service_onboarding import (
    get_onboarding_state,
    mark_onboarding_skipped,
    set_onboarding_step,
)

router = APIRouter(prefix="/auth/onboarding", tags=["auth"])

ONBOARDING_STEPS = (1, 2, 3, 4)


@router.get("", response_model=ApiResp[OnboardingState])
@respond
async def get_onboarding(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> OnboardingState:
    """读取当前用户引导进度；未开始时返回默认 step=1，不 404。"""
    return await get_onboarding_state(db, cur.id)


@router.put("/steps/{step}", response_model=ApiResp[OnboardingState])
@respond
async def put_onboarding_step(
    step: int,
    body: OnboardingStepRequest,
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> OnboardingState:
    """提交某一步的分步数据并推进到该步。"""
    if step not in ONBOARDING_STEPS:
        raise BizError(CommonErr.INVALID_INPUT, "Step out of range")
    return await set_onboarding_step(db, cur.id, step, body.data)


@router.post("/skip", response_model=ApiResp[OnboardingState])
@respond
async def skip_onboarding(
    cur: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_auth_session),
) -> OnboardingState:
    """整体跳过引导并视为完成。"""
    return await mark_onboarding_skipped(db, cur.id)
