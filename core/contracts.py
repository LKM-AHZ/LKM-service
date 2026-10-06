"""
跨包共享契约：纯数据模型（无 auth 内部依赖）。
这些类型原先定义在 auth 侧（``auth.deps.CurrentUser`` / ``auth.schemas`` / ``auth.snapshot``），
被业务侧大量用作类型标注与 DTO 组装——为一个纯 Pydantic 类型去 import auth 会把
auth 的 DB/security 整链拉进 app 进程。故下沉到 core，auth 侧改为重导出。
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


class ProfileRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


def _validate_password(v: str) -> str:
    if len(v) < 6:
        raise ValueError("Password must be at least 6 characters")
    return v


Password = Annotated[str, AfterValidator(_validate_password)]


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_preserve_case(v: str) -> str:
    """
    校验邮箱格式但大小写绝对敏感。
    Pydantic 内置 ``EmailStr`` 会把 domain 强制转小写，违背本项目「存储原值 + 精确匹配」
    的大小写绝对敏感约定，故此处自定义：仅去首尾空白 + 宽松格式校验，不做任何小写转换。
    """
    v = v.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email format")
    return v


RawEmail = Annotated[str, AfterValidator(_validate_email_preserve_case)]


class ContactLink(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    icon: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value and (
            "\\" in value
            or (
                not (value.startswith("/") and not value.startswith("//"))
                and not value.lower().startswith(("http://", "https://"))
            )
        ):
            raise ValueError("Contact link URL must be http(s) or a relative path")
        return value


class ProfileInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    nickname: str | None = None
    avatar: str | None = None
    role: str = ProfileRole.MEMBER
    contact_links: list[ContactLink] = Field(default_factory=list)


class ProfileUpdate(BaseModel):
    # 上界与 profiles.nickname 列（String(100)）一致
    nickname: str | None = Field(None, max_length=100)
    avatar: str | None = None
    contact_links: list[ContactLink] | None = Field(default=None, max_length=20)


class CurrentUser(BaseModel):
    """从已验证的 JWT 访问令牌中提取的用户信息（鉴权依赖的返回值契约）。"""

    id: uuid.UUID
    account_level: str
    role: str
    active_roles: tuple[str, ...] | None = None
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class UserSnapshot:
    """
    业务侧固定身份读模型。不含 email/phone/凭证 等 PII/敏感列。
    """

    user_id: uuid.UUID
    username: str
    display_name: str
    avatar: str | None
    role: str | None
    account_level: str
    banned: bool
    nickname: str | None


@dataclass(frozen=True)
class UserManagementItem:
    """
    后台管理面用户行：管理列(id/username/account_level/is_locked/created_at)恒定。
    """

    id: uuid.UUID
    username: str
    account_level: str
    is_locked: bool
    created_at: datetime.datetime
    # PII（默认隐藏；include_pii=True 才填充）
    email: str | None = None
    phone: str | None = None


def profile_info_from_snap(snap: UserSnapshot) -> ProfileInfo | None:
    """
    快照缝 → ``ProfileInfo`` DTO（blog/articles 组装作者资料时脱离直读 Profile）。
    """
    if snap.role is None:
        return None
    try:
        role = ProfileRole(snap.role)
    except ValueError:
        role = ProfileRole.MEMBER
    return ProfileInfo(nickname=snap.nickname, avatar=snap.avatar, role=role)
