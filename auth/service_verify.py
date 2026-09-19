"""验证码服务：生成、哈希、创建、消费、速率限制。"""

import datetime
import hashlib
import hmac
import secrets
import uuid
from typing import Any, cast

from app.core.config import settings
from app.core.err import BizError
from app.core.redis_limiter import RedisRateLimiter
from app.core.secrets import reveal
from app.db.base import now_iso
from app.db.repo import consume_once, isolated_update
from app.db.repository import DbSession
from auth.errors import AuthErr
from auth.models import EmailVerification, PhoneVerification
from auth.repository import VerificationRepository

_CODE_EXPIRE_MINUTES = 10
_MAX_FAILED_ATTEMPTS = 3


def generate_code() -> str:
    """返回一个 6 位数字验证码，格式为零填充字符串。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(raw: str, purpose: str = "", contact: str = "", nonce: str = "") -> str:
    pepper = reveal(settings.verification_code_pepper).encode()
    msg = f"{raw}:{purpose}:{contact}:{nonce}".encode()
    return hmac.new(pepper, msg, hashlib.sha256).hexdigest()


async def _create_verification(
    db: DbSession,
    model: type[Any],
    contact_attr: str,
    contact: str,
    purpose: str,
) -> tuple[str, uuid.UUID]:
    """创建一条验证码记录并返回 (明文验证码, 记录ID)。"""
    code = generate_code()
    nonce = secrets.token_hex(8)
    record = await VerificationRepository(db, model).create(
        **{contact_attr: contact},
        code_hash=hash_code(code, purpose, contact=contact, nonce=nonce),
        nonce=nonce,
        purpose=purpose,
        expires_at=_expires_at(),
    )
    return code, record.id


async def _latest_verification(
    db: DbSession,
    model: type[Any],
    contact_attr: str,
    contact: str,
    purpose: str,
) -> Any:
    """取该联系方式未使用的最新一条验证码记录。"""
    return await VerificationRepository(db, model, contact_attr).latest(
        contact, purpose
    )


async def create_email_verification(
    db: DbSession, email: str, purpose: str
) -> tuple[str, uuid.UUID]:
    """创建一个 EmailVerification 记录并返回 (明文验证码, 记录ID)。"""
    return await _create_verification(db, EmailVerification, "email", email, purpose)


async def create_phone_verification(
    db: DbSession, phone: str, purpose: str
) -> tuple[str, uuid.UUID]:
    """创建一个 PhoneVerification 记录并返回 (明文验证码, 记录ID)。"""
    return await _create_verification(db, PhoneVerification, "phone", phone, purpose)


async def consume_email_code(
    db: DbSession, email: str, code: str, purpose: str
) -> bool:
    """验证并消费最新匹配的 EmailVerification。"""
    record = await _latest_verification(db, EmailVerification, "email", email, purpose)
    return await _consume(db, record, code)


async def consume_phone_code(
    db: DbSession, phone: str, code: str, purpose: str
) -> bool:
    """验证并消费最新匹配的 PhoneVerification。"""
    record = await _latest_verification(db, PhoneVerification, "phone", phone, purpose)
    return await _consume(db, record, code)


async def _consume(db: DbSession, record: Any, code: str) -> bool:
    if record is None:
        raise BizError(AuthErr.VERIFICATION_CODE_INVALID)

    now = now_iso()

    # 过期检查
    if record.expires_at <= now:
        raise BizError(AuthErr.VERIFICATION_CODE_EXPIRED)

    # 失败尝试次数过多
    if record.failed_attempts >= _MAX_FAILED_ATTEMPTS:
        raise BizError(AuthErr.VERIFICATION_CODE_INVALID)

    # 验证码不匹配 —— 通过子事务（保存点）递增计数器，
    contact = getattr(record, "email", None) or getattr(record, "phone", "")
    if not hmac.compare_digest(
        record.code_hash,
        hash_code(code, record.purpose, contact=contact, nonce=record.nonce),
    ):
        record_cls = cast(type[Any], type(record))
        await isolated_update(
            db, VerificationRepository(db, record_cls).failed_stmt(record.id)
        )
        await db.refresh(record)
        raise BizError(AuthErr.VERIFICATION_CODE_INVALID)

    record_cls = cast(type[Any], type(record))
    if not await consume_once(
        db,
        record_cls,
        {"used": True},
        record_cls.id == record.id,
        record_cls.used.is_(False),
        record_cls.failed_attempts < _MAX_FAILED_ATTEMPTS,
        record_cls.expires_at > now,
    ):
        raise BizError(AuthErr.VERIFICATION_CODE_INVALID)

    return True


async def check_code_rate_limit(
    key: str, max_count: int = 5, window: float = 3600
) -> None:
    """如果 *key* 超过了限制，则抛出 ``BizError(VERIFICATION_CODE_RATE_LIMIT)``。

    Redis 已在栈上（``redis_url`` 非空）但运行期不可用时 **拒绝（fail-close）**：
    本函数守卫登录/验证码/2FA/恢复等暴力破解关键路径，若在依赖抖动瞬间 fail-open，
    等于防线直接消失，宁可短暂拒绝。而未配置 Redis（``redis_url`` 为空）时没有分布式
    限流依赖可失败，放行保持原语义（那种部署本就不靠 Redis 防爆破，靠 DB 级锁定兜底）。
    """
    # 未配置 Redis：无分布式限流可失败，非"运行期抖动"，按原行为放行。
    if not reveal(settings.redis_url):
        return
    if not await RedisRateLimiter().check(key, max_count, window, fail_open=False):
        raise BizError(AuthErr.VERIFICATION_CODE_RATE_LIMIT)


def _expires_at() -> datetime.datetime:
    return now_iso() + datetime.timedelta(minutes=_CODE_EXPIRE_MINUTES)
