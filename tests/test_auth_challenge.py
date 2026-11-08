"""挑战-因子轮转：状态机、设备绑定、错误码与「不泄露账号存在性」。

覆盖的是 :mod:`auth.challenge` 的编排语义——真正验密仍由 ``login_password`` 负责，
所以这里只断言「挑战怎么推进」，不断言密码策略本身（那有独立用例）。
"""

import datetime
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from auth.challenge import (
    FACTOR_CODE,
    FACTOR_PASSWORD,
    cancel_challenge,
    create_challenge,
    fingerprint,
    issue_login_code,
    load_challenge,
    state_payload,
    submit_factor,
)
from auth.errors import AuthErr
from auth.models import Profile, User
from auth.security import hashpwd
from core.db.base import now_iso
from core.err import BizError

_PASSWORD = "secret123456"


async def _mk_user(db: AsyncSession, username: str = "alma") -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd(_PASSWORD),
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


def _errcode(exc: pytest.ExceptionInfo[BizError]) -> int:
    return int(exc.value.errcode)


class TestChallengeLifecycle:
    async def test_create_exposes_only_the_first_factor(
        self, auth_db: AsyncSession
    ) -> None:
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        payload = state_payload(challenge)
        assert payload["status"] == "pending"
        assert payload["factors"] == [{"kind": FACTOR_PASSWORD}]
        # 只存哈希，不存原始 device_id。
        assert challenge.device_hash == fingerprint("device-a")
        assert challenge.device_hash != "device-a"

    async def test_password_submit_returns_tokens_and_consumes(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db)
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        result = await submit_factor(
            auth_db,
            challenge,
            kind=FACTOR_PASSWORD,
            password=_PASSWORD,
        )

        assert result["status"] == "complete"
        assert result["access_token"]
        assert result["refresh_token"]
        assert result["account_level"]
        assert uuid.UUID(result["user_id"])

        await auth_db.refresh(challenge)
        assert challenge.consumed is True
        assert challenge.status == "complete"
        assert challenge.user_id is not None

    async def test_completed_challenge_cannot_be_replayed(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db)
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )
        await submit_factor(
            auth_db, challenge, kind=FACTOR_PASSWORD, password=_PASSWORD
        )

        with pytest.raises(BizError) as exc:
            await load_challenge(auth_db, challenge.id, "device-a")
        assert _errcode(exc) == AuthErr.CHALLENGE_CONSUMED

    async def test_wrong_password_leaves_the_challenge_pending(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db)
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        with pytest.raises(BizError) as exc:
            await submit_factor(
                auth_db, challenge, kind=FACTOR_PASSWORD, password="wrong-password"
            )
        assert _errcode(exc) == AuthErr.INVALID_CREDENTIALS

        await auth_db.refresh(challenge)
        assert challenge.status == "pending"
        assert challenge.consumed is False
        assert challenge.attempts == 1

    async def test_unknown_account_fails_only_at_submit(
        self, auth_db: AsyncSession
    ) -> None:
        # 建挑战不查库：不存在的账号也能建，形状与真实账号一致（不可枚举）。
        challenge = await create_challenge(
            auth_db, account="nobody", device_id="device-a"
        )
        assert state_payload(challenge)["factors"] == [{"kind": FACTOR_PASSWORD}]

        with pytest.raises(BizError) as exc:
            await submit_factor(
                auth_db, challenge, kind=FACTOR_PASSWORD, password=_PASSWORD
            )
        assert _errcode(exc) == AuthErr.INVALID_CREDENTIALS

    async def test_code_challenge_hides_account_existence(
        self, auth_db: AsyncSession
    ) -> None:
        challenge = await create_challenge(
            auth_db,
            account="nobody@example.com",
            device_id="device-a",
            primary_factor=FACTOR_CODE,
        )

        assert await issue_login_code(auth_db, challenge) is None
        assert state_payload(challenge)["factors"] == [{"kind": FACTOR_CODE}]


class TestChallengeGuards:
    async def test_device_mismatch_looks_like_a_missing_challenge(
        self, auth_db: AsyncSession
    ) -> None:
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        with pytest.raises(BizError) as mismatch:
            await load_challenge(auth_db, challenge.id, "device-b")
        with pytest.raises(BizError) as missing:
            await load_challenge(auth_db, uuid.uuid4(), "device-a")

        # 同码：区分开就是「这个 id 是真的，只是设备不对」的探测面。
        assert _errcode(mismatch) == _errcode(missing) == (
            AuthErr.CHALLENGE_NOT_FOUND
        )

    async def test_expired_challenge_is_rejected(
        self, auth_db: AsyncSession
    ) -> None:
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )
        challenge.expires_at = now_iso() - datetime.timedelta(seconds=1)
        await auth_db.commit()

        with pytest.raises(BizError) as exc:
            await load_challenge(auth_db, challenge.id, "device-a")
        assert _errcode(exc) == AuthErr.CHALLENGE_EXPIRED

    async def test_out_of_order_factor_is_rejected(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db)
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        with pytest.raises(BizError) as exc:
            await submit_factor(auth_db, challenge, kind="totp", code="123456")
        assert _errcode(exc) == AuthErr.CHALLENGE_STEP_INVALID

    async def test_too_many_attempts_consumes_the_challenge(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db)
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )
        challenge.attempts = 5
        await auth_db.commit()

        with pytest.raises(BizError) as exc:
            await submit_factor(
                auth_db, challenge, kind=FACTOR_PASSWORD, password=_PASSWORD
            )
        assert _errcode(exc) == AuthErr.CHALLENGE_TOO_MANY_ATTEMPTS

        await auth_db.refresh(challenge)
        assert challenge.consumed is True
        assert challenge.status == "cancelled"

    async def test_cancel_closes_the_challenge(self, auth_db: AsyncSession) -> None:
        challenge = await create_challenge(
            auth_db, account="alma", device_id="device-a"
        )

        await cancel_challenge(auth_db, challenge)

        with pytest.raises(BizError):
            await load_challenge(auth_db, challenge.id, "device-a")
