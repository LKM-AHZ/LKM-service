"""Tests for onboarding progress endpoints (router_onboarding.py).

Covers:
- GET onboarding returns default step=1 when no progress
- PUT step merges data and advances step
- POST skip marks completed and sets step=4
- Step out of range → INVALID_INPUT
"""

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from auth.deps import CurrentUser
from auth.router_onboarding import (
    get_onboarding,
    put_onboarding_step,
    skip_onboarding,
)
from auth.schemas import OnboardingStepRequest


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """onboarding 表在 auth 独立库（S5 拆后）；本文件单一 auth schema 面上跑即可。"""
    return auth_db


def _FakeCurrentUser(id: int, account_level: str = "normal") -> CurrentUser:
    return CurrentUser(id=id, account_level=account_level, role="member")


def _unwrap(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode())


async def _make_user(db: AsyncSession, username: str = "onboarder") -> CurrentUser:
    from auth.models import Profile, User

    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return _FakeCurrentUser(user.id)


class TestGetOnboarding:
    async def should_return_default_when_no_progress(self, db: AsyncSession):
        cur = await _make_user(db)
        data = _unwrap(await get_onboarding(cur=cur, db=db))
        d = data["data"]
        assert d["step"] == 1
        assert d["completed"] is False
        assert d["data"] is None

    async def should_return_saved_progress(self, db: AsyncSession):
        cur = await _make_user(db)
        _unwrap(
            await put_onboarding_step(
                step=2,
                body=OnboardingStepRequest(data={"grade": "math"}),
                cur=cur,
                db=db,
            )
        )
        data = _unwrap(await get_onboarding(cur=cur, db=db))
        d = data["data"]
        assert d["step"] == 2
        assert d["data"] == {"2": {"grade": "math"}}


class TestPutOnboardingStep:
    async def should_merge_data_per_step(self, db: AsyncSession):
        cur = await _make_user(db)
        _unwrap(
            await put_onboarding_step(
                step=1,
                body=OnboardingStepRequest(data={"grade": "math"}),
                cur=cur,
                db=db,
            )
        )
        _unwrap(
            await put_onboarding_step(
                step=2,
                body=OnboardingStepRequest(data={"interests": ["crypto"]}),
                cur=cur,
                db=db,
            )
        )
        data = _unwrap(await get_onboarding(cur=cur, db=db))
        d = data["data"]
        assert d["data"] == {"1": {"grade": "math"}, "2": {"interests": ["crypto"]}}
        assert d["step"] == 2

    async def should_overwrite_same_step(self, db: AsyncSession):
        cur = await _make_user(db)
        _unwrap(
            await put_onboarding_step(
                step=1,
                body=OnboardingStepRequest(data={"grade": "math"}),
                cur=cur,
                db=db,
            )
        )
        _unwrap(
            await put_onboarding_step(
                step=1,
                body=OnboardingStepRequest(data={"grade": "physics"}),
                cur=cur,
                db=db,
            )
        )
        data = _unwrap(await get_onboarding(cur=cur, db=db))
        assert data["data"]["data"] == {"1": {"grade": "physics"}}

    async def should_reject_step_out_of_range(self, db: AsyncSession):
        cur = await _make_user(db)
        with pytest.raises(Exception) as exc:
            await put_onboarding_step(
                step=5,
                body=OnboardingStepRequest(data={}),
                cur=cur,
                db=db,
            )
        from app.core.err import BizError

        assert isinstance(exc.value, BizError)
        assert exc.value.errcode == CommonErr.INVALID_INPUT


class TestSkipOnboarding:
    async def should_mark_completed(self, db: AsyncSession):
        cur = await _make_user(db)
        data = _unwrap(await skip_onboarding(cur=cur, db=db))
        d = data["data"]
        assert d["completed"] is True
        assert d["step"] == 4
