import datetime as dt
import re
import uuid
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import EmailVerification, PhoneVerification
from app.modules.auth.service_verify import (
    check_code_rate_limit,
    consume_email_code,
    consume_phone_code,
    create_email_verification,
    create_phone_verification,
    generate_code,
    hash_code,
)


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """auth 域单测跑在 auth 独立库 schema（S5 拆后 users 不在 biz）。"""
    return auth_db


async def _get[T](db: AsyncSession, model: type[T], *where: Any) -> T:
    # 测试均为“先建后查”，必然命中，返回类型直接按 _T 处理
    return cast(T, (await db.execute(select(model).where(*where))).scalars().first())


class TestGenerateCode:
    def should_generate_six_digit_code(self):
        code = generate_code()
        assert re.fullmatch(r"\d{6}", code)

    def should_generate_different_codes(self):
        codes = {generate_code() for _ in range(100)}
        assert len(codes) > 1


class TestHashCode:
    def should_be_hmac_hex_string(self):
        raw = "123456"
        hashed = hash_code(raw, "register", contact="test@x.com", nonce="abc123")
        assert len(hashed) == 64  # HMAC-SHA256 => 64 hex chars

    def should_be_deterministic(self):
        assert hash_code(
            "000000", "register", contact="a@b.com", nonce="n1"
        ) == hash_code("000000", "register", contact="a@b.com", nonce="n1")

    def should_differ_for_different_inputs(self):
        assert hash_code("000000", "login", contact="a@b.com", nonce="n1") != hash_code(
            "000001", "login", contact="a@b.com", nonce="n1"
        )

    def should_differ_for_different_purposes(self):
        assert hash_code("123456", "login", contact="a@b.com", nonce="n1") != hash_code(
            "123456", "register", contact="a@b.com", nonce="n1"
        )

    def should_differ_for_different_nonces(self):
        assert hash_code("123456", "login", contact="a@b.com", nonce="n1") != hash_code(
            "123456", "login", contact="a@b.com", nonce="n2"
        )


class TestCreateEmailVerification:
    async def should_create_verification_and_return_code_and_id(self, db: AsyncSession):
        code, record_id = await create_email_verification(
            db, "alice@example.com", "register"
        )

        assert re.fullmatch(r"\d{6}", code)
        assert isinstance(record_id, uuid.UUID)

        record = await _get(db, EmailVerification, EmailVerification.id == record_id)
        assert record is not None
        assert record.email == "alice@example.com"
        assert record.purpose == "register"
        assert record.code_hash == hash_code(
            code,
            record.purpose,
            contact=cast(Any, record).email
            if hasattr(record, "email")
            else cast(Any, record).phone,
            nonce=record.nonce,
        )
        assert record.used is False
        assert record.failed_attempts == 0


class TestCreatePhoneVerification:
    async def should_create_verification_and_return_code_and_id(self, db: AsyncSession):
        code, record_id = await create_phone_verification(db, "13800138000", "login")

        assert re.fullmatch(r"\d{6}", code)
        assert isinstance(record_id, uuid.UUID)

        record = await _get(db, PhoneVerification, PhoneVerification.id == record_id)
        assert record is not None
        assert record.phone == "13800138000"
        assert record.purpose == "login"
        assert record.code_hash == hash_code(
            code,
            record.purpose,
            contact=cast(Any, record).email
            if hasattr(record, "email")
            else cast(Any, record).phone,
            nonce=record.nonce,
        )
        assert record.used is False
        assert record.failed_attempts == 0


class TestConsumeEmailCode:
    async def should_consume_correct_code(self, db: AsyncSession):
        code, record_id = await create_email_verification(
            db, "alice@example.com", "register"
        )
        result = await consume_email_code(db, "alice@example.com", code, "register")
        assert result is True

        record = await _get(db, EmailVerification, EmailVerification.id == record_id)
        assert record.used is True

    async def should_reject_wrong_purpose(self, db: AsyncSession):
        code, _ = await create_email_verification(db, "alice@example.com", "register")

        with pytest.raises(BizError) as exc:
            await consume_email_code(db, "alice@example.com", code, "login")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_reject_wrong_code(self, db: AsyncSession):
        await create_email_verification(db, "alice@example.com", "register")

        with pytest.raises(BizError) as exc:
            await consume_email_code(db, "alice@example.com", "000000", "register")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_invalidate_after_three_failed_attempts(self, db: AsyncSession):
        code, _ = await create_email_verification(db, "alice@example.com", "register")

        for _ in range(3):
            with pytest.raises(BizError) as exc:
                await consume_email_code(db, "alice@example.com", "000001", "register")
            assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

        # The original code should be invalid now
        with pytest.raises(BizError) as exc:
            await consume_email_code(db, "alice@example.com", code, "register")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_not_consume_expired_code(self, db: AsyncSession):
        with patch("app.modules.auth.service_verify.now_iso") as mock_now:
            mock_now.return_value = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            code, _ = await create_email_verification(
                db, "alice@example.com", "register"
            )

        with patch("app.modules.auth.service_verify.now_iso") as mock_now:
            mock_now.return_value = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
            with pytest.raises(BizError) as exc:
                await consume_email_code(db, "alice@example.com", code, "register")
            assert exc.value.errcode == AuthErr.VERIFICATION_CODE_EXPIRED


class TestConsumePhoneCode:
    async def should_consume_correct_code(self, db: AsyncSession):
        code, record_id = await create_phone_verification(db, "13800138000", "login")
        result = await consume_phone_code(db, "13800138000", code, "login")
        assert result is True

        record = await _get(db, PhoneVerification, PhoneVerification.id == record_id)
        assert record.used is True

    async def should_reject_wrong_purpose(self, db: AsyncSession):
        code, _ = await create_phone_verification(db, "13800138000", "login")

        with pytest.raises(BizError) as exc:
            await consume_phone_code(db, "13800138000", code, "register")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_reject_wrong_code(self, db: AsyncSession):
        await create_phone_verification(db, "13800138000", "login")

        with pytest.raises(BizError) as exc:
            await consume_phone_code(db, "13800138000", "000000", "login")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_invalidate_after_three_failed_attempts(self, db: AsyncSession):
        code, _ = await create_phone_verification(db, "13800138000", "login")

        for _ in range(3):
            with pytest.raises(BizError) as exc:
                await consume_phone_code(db, "13800138000", "000001", "login")
            assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

        with pytest.raises(BizError) as exc:
            await consume_phone_code(db, "13800138000", code, "login")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_not_consume_expired_code(self, db: AsyncSession):
        with patch("app.modules.auth.service_verify.now_iso") as mock_now:
            mock_now.return_value = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            code, _ = await create_phone_verification(db, "13800138000", "login")

        with patch("app.modules.auth.service_verify.now_iso") as mock_now:
            mock_now.return_value = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
            with pytest.raises(BizError) as exc:
                await consume_phone_code(db, "13800138000", code, "login")
            assert exc.value.errcode == AuthErr.VERIFICATION_CODE_EXPIRED


class TestCheckCodeRateLimit:
    """验证码限流的 fail-close 行为（Redis 运行期不可用拒绝，不放开爆破面）。

    - Redis 未配置（redis_url 空）：无分布式限流可失败，放行（保持原语义）。
    - Redis 已配置但运行期不可用（get_redis 返回 None）：拒绝（fail-close）。

    滑动窗口语义（超限抛 BizError / reset / 隔离 / 窗口过期）依赖真实 Redis 的
    Lua 脚本，由集成测试 tests/integration/test_redis_limiter_integration.py 覆盖。
    """

    async def should_pass_when_redis_unconfigured(self) -> None:
        """LKM_REDIS_URL 为空 → 放行，不抛异常（无分布式限流依赖可失败）。"""
        from app.core.config import settings

        original_url = settings.redis_url
        settings.redis_url = ""
        try:
            await check_code_rate_limit("test@example.com", max_count=5, window=3600)
            # 不应抛 BizError
        finally:
            settings.redis_url = original_url

    async def should_fail_close_when_redis_configured_but_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LKM_REDIS_URL 已配置但运行期不可用（get_redis 返回 None）→ 拒绝。

        Redis 抖动瞬间 fail-open 等于放开暴力破解面，这里宁可拒绝。
        """
        from app.core import redis as redis_core
        from app.core.config import settings

        original = redis_core.get_redis

        async def _none() -> Any:
            return None

        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        redis_core.get_redis = _none  # ty: ignore[invalid-assignment]  # runtime monkeypatch
        try:
            with pytest.raises(BizError) as exc_info:
                await check_code_rate_limit(
                    "test@example.com", max_count=5, window=3600
                )
            assert exc_info.value.errcode == AuthErr.VERIFICATION_CODE_RATE_LIMIT
        finally:
            redis_core.get_redis = original  # type: ignore[assignment]
