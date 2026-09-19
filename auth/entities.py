"""auth 包公开的 ORM 实体类型（供业务 seed 与运维脚本使用）。

业务库 ``Base.metadata`` 与 auth 库 ``AuthBase`` 无交叉外键，两侧 mapper 解析互不依赖；
但业务 seed 与运维脚本需要 User/Profile 等实体类做数据种入，故**精确**挑出这些类型作为
公开面，而不是放开整个 ``auth.models``（内部映射仍属私有）。

首次 ORM 操作前须确保模型已注册：``auth.register_models()``（幂等）。
"""

from __future__ import annotations

from auth.models import (
    TOTP,
    Profile,
    RecoveryCode,
    RefreshToken,
    User,
)

__all__ = ["TOTP", "Profile", "RecoveryCode", "RefreshToken", "User"]
