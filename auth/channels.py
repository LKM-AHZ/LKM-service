"""联系通道策略表 —— 把「邮箱 vs 手机号」的分发收敛到这一张表里。

每个通道封装自己的判定、规范化、查找、验证码创建/消费与发送。
调用方只写 ``CHANNELS[detect(contact)]`` 或 ``channel_for(contact)``，
「@ 判断邮箱」这个启发式只存在于 :func:`detect` 一处。
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.err import BizError, CommonErr
from auth.deps import get_email_provider, get_sms_provider
from auth.models import User
from auth.service_verify import (
    consume_email_code,
    consume_phone_code,
    create_email_verification,
    create_phone_verification,
)


class CodeSender(Protocol):
    """验证码发送器 —— 邮箱与短信提供商都满足。"""

    async def send_code(self, contact: str, code: str) -> None: ...


@dataclass(frozen=True)
class ContactChannel:
    name: str
    normalize: Callable[[str], str]
    username_from: Callable[[str], str]
    find_user: Callable[[AsyncSession, str], Awaitable[User | None]]
    # 返回 (code/txn 标识, 验证记录 id)：service_verify 的两个实现返回 tuple[str, UUID]
    create_verification: Callable[[AsyncSession, str, str], Awaitable[tuple[str, uuid.UUID]]]
    consume_code: Callable[[AsyncSession, str, str, str], Awaitable[bool]]
    send_code: Callable[[str, str], Awaitable[None]]


async def _find_email_user(db: AsyncSession, value: str) -> User | None:
    return (
        (
            await db.execute(
                select(User)
                .where(User.email == value)
                .options(selectinload(User.profile))
            )
        )
        .scalars()
        .first()
    )


async def _find_phone_user(db: AsyncSession, value: str) -> User | None:
    return (
        (
            await db.execute(
                select(User)
                .where(User.phone == value)
                .options(selectinload(User.profile))
            )
        )
        .scalars()
        .first()
    )


def _email_normalize(value: str) -> str:
    """邮箱规范化：仅去首尾空白，大小写原样保留（大小写绝对敏感）。"""
    return value.strip()


def _phone_normalize(value: str) -> str:
    """手机号规范化：与邮箱同口径，仅去首尾空白。

    原先直接原样返回，导致 " 13800138000 " 这类带空白的输入被拿去查库/发短信。
    """
    return value.strip()


def _email_username_from(value: str) -> str:
    return value.split("@")[0]


def _phone_username_from(value: str) -> str:
    return f"user_{value[-6:]}"


async def _email_send_code(contact: str, code: str) -> None:
    await get_email_provider().send_code(contact, code)


async def _phone_send_code(contact: str, code: str) -> None:
    await get_sms_provider().send_code(contact, code)


EMAIL_CHANNEL = ContactChannel(
    name="email",
    normalize=_email_normalize,
    username_from=_email_username_from,
    find_user=_find_email_user,
    create_verification=create_email_verification,
    consume_code=consume_email_code,
    send_code=_email_send_code,
)

PHONE_CHANNEL = ContactChannel(
    name="phone",
    normalize=_phone_normalize,
    username_from=_phone_username_from,
    find_user=_find_phone_user,
    create_verification=create_phone_verification,
    consume_code=consume_phone_code,
    send_code=_phone_send_code,
)

CHANNELS: dict[str, ContactChannel] = {
    EMAIL_CHANNEL.name: EMAIL_CHANNEL,
    PHONE_CHANNEL.name: PHONE_CHANNEL,
}


def detect(contact: str) -> str:
    """邮箱还是手机号 —— 整个代码库唯一的 '@' 启发式。空/纯空白直接拒。"""
    if not contact or not contact.strip():
        raise BizError(CommonErr.INVALID_INPUT, "contact must not be empty")
    return "email" if "@" in contact else "phone"


def channel_for(contact: str) -> ContactChannel:
    return CHANNELS[detect(contact)]
