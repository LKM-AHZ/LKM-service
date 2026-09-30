"""跨包共享契约：纯数据模型（无 auth 内部依赖、无副作用）。

这些类型原先定义在 auth 侧（``auth.deps.CurrentUser`` / ``auth.schemas`` / ``auth.snapshot``），
被业务侧大量用作类型标注与 DTO 组装——为一个纯 Pydantic 类型去 import auth 会把
auth 的 DB/security 整链拉进 app 进程。故下沉到 core，auth 侧改为重导出。

本模块只依赖标准库 + pydantic：不 import core.db / core.ports，保持「契约零副作用」。
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- 档案与角色


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
    """校验邮箱格式但**保留大小写原样**（大小写绝对敏感）。

    Pydantic 内置 ``EmailStr`` 会把 domain 强制转小写，违背本项目「存储原值 + 精确匹配」
    的大小写绝对敏感约定，故此处自定义：仅去首尾空白 + 宽松格式校验，不做任何小写转换。
    """
    v = v.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email format")
    return v


RawEmail = Annotated[str, AfterValidator(_validate_email_preserve_case)]


class ProfileInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    nickname: str | None = None
    avatar: str | None = None
    role: ProfileRole = ProfileRole.MEMBER


class ProfileUpdate(BaseModel):
    # 上界与 profiles.nickname 列（String(100)）一致：不设会在 PG 侧抛
    # StringDataRightTruncation 变 500，而不是作为非法入参被拒。
    nickname: str | None = Field(None, max_length=100)
    avatar: str | None = None


# ---------------------------------------------------------------- 身份读模型


class CurrentUser(BaseModel):
    """从已验证的 JWT 访问令牌中提取的用户信息（鉴权依赖的返回值契约）。"""

    id: uuid.UUID
    account_level: str
    role: str
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class UserSnapshot:
    """业务侧固定身份读模型。不含 email/phone/凭证 等 PII/敏感列。

    ``nickname``（raw，**非 PII**，同 username/display_name 属展示身份列）为 profiles.nickname
    的逐字照搬：空白时是 None —— **不回退到 username**。这与 ``display_name``（nickname or
    username 合成）刻意保持**语义分流**：需要"展示名默认回退"的用 display_name；需要"nickname
    是否真被设置"（如 blog/articles 组 ProfileInfo 须保 blank-when-unset）的读 raw nickname。
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
    """后台管理面用户行：管理列(id/username/account_level/is_locked/created_at)恒定。

    刻意**不是**展示型 ``UserSnapshot``：管理行是 admin 治理列表的必要字段（created_at/
    is_locked 等展示缝不暴露），且可条件承载 PII。email/phone 字段在 ``include_pii=False``
    的投影路径**根本不被 SELECT**，恒为 None（默认构造不读写 User 的 PII 列）；只有
    ``include_pii=True`` 的项目才填充。本类型仅供管理授权读，不入展示缝——评论侧等展示
    消费方接触到的仍是零 PII 的 ``UserSnapshot``。
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
    """快照缝 → ``ProfileInfo`` DTO（blog/articles 组装作者资料时脱离直读 Profile）。

    - ``nickname`` 取 snap 的 **raw nickname**（原样，空白即 None），**且不回退 username** ——
      保持 blog/articles ProfileInfo blank-when-unset 语义（这与 display_name 的合成回退刻意分流）。
    - ``role`` 由 snap.role 字符串转 ``ProfileRole``（None → 默认 MEMBER）。
    - 无 Profile 的用户（snap.role 缺失 → 原 repo 路径不产出 ProfileInfo）返回 None，保持原先
      ``profiles.get(uid)`` 对 no-profile 用户落 None 的语义；其余字段 avatar/nickname 逐字节照搬。
    ``ProfileRole``/nickname/avatar 均非 PII，展示方本就承载；本转换不含 email/phone/凭证。
    """
    if snap.role is None:  # profile.role 非空(NOT NULL default='member')，None 即无 Profile 行
        return None
    try:
        role = ProfileRole(snap.role)
    except ValueError:
        role = ProfileRole.MEMBER
    return ProfileInfo(nickname=snap.nickname, avatar=snap.avatar, role=role)
