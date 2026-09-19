from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import UTCDateTime, UUIDPrimaryKeyMixin, now_iso
from auth.db.base import AuthBase


class RefreshToken(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "refresh_tokens"
    __table_args__: tuple[Any, ...] = (
        Index("ix_refresh_tokens_user_revoked", "user_id", "revoked_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 会话用途："web"（前台 Bearer）/"admin"（后台 cookie）。用于隔离，避免跨会话互用。
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="web")
    mfa_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # step-up 2FA 信任原点（epoch）：随刷新轮换继承，保留 1 小时信任窗口不被 15min access 轮换重置
    mfa_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class EmailVerification(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "email_verifications"
    __table_args__: tuple[Any, ...] = (
        Index(
            "ix_email_verifications_lookup", "email", "purpose", "used", "created_at"
        ),
    )

    email: Mapped[str] = mapped_column(String(200), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    nonce: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class PhoneVerification(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "phone_verifications"
    __table_args__: tuple[Any, ...] = (
        Index(
            "ix_phone_verifications_lookup", "phone", "purpose", "used", "created_at"
        ),
    )

    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    nonce: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class MagicLink(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "magic_links"
    __table_args__: tuple[Any, ...] = (
        Index("ix_magic_links_hash_purpose", "token_hash", "purpose"),
    )

    email: Mapped[str] = mapped_column(String(200), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class UserOAuth(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "user_oauths"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_user"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    user: Mapped[User] = relationship(back_populates="oauth_bindings")


class TOTP(AuthBase):
    __tablename__: str = "totp"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirmed_saved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    user: Mapped[User] = relationship(back_populates="totp")


class RecoveryCode(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "recovery_codes"
    __table_args__: tuple[Any, ...] = (
        Index("ix_recovery_codes_user_hash", "user_id", "code_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    user: Mapped[User] = relationship(back_populates="recovery_codes")


class TempTokenUsage(UUIDPrimaryKeyMixin, AuthBase):
    """跟踪用于 2FA 验证的一次性临时令牌。"""

    __tablename__: str = "temp_token_usages"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="2fa")
    txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class SetupTransaction(UUIDPrimaryKeyMixin, AuthBase):
    """绑定到设置临时令牌的一次性 TOTP 设置事务。"""

    __tablename__: str = "setup_transactions"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class PendingRegistration(UUIDPrimaryKeyMixin, AuthBase):
    """存储待处理的普通注册数据，直到联系方式被验证。"""

    __tablename__: str = "pending_registrations"

    txn_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class RecoveryTransaction(UUIDPrimaryKeyMixin, AuthBase):
    """专用的管理员密码恢复事务，支持双重验证。"""

    __tablename__: str = "recovery_transactions"

    txn_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    contact: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    totp_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="contact_pending"
    )
    failed_contact_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    failed_second_factor_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    failed_setup_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    recovery_jti_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class PasskeyCredential(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "passkey_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    device_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    user: Mapped[User] = relationship(back_populates="passkey_credentials")


class AuditLog(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class PasskeyChallenge(UUIDPrimaryKeyMixin, AuthBase):
    """WebAuthn 挑战码，跨 worker 共享，过期自动失效。"""

    __tablename__: str = "passkey_challenges"

    challenge_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    challenge: Mapped[str] = mapped_column(Text, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class OAuthState(UUIDPrimaryKeyMixin, AuthBase):
    """临时的 OAuth 状态令牌，用于 CSRF 保护。"""

    __tablename__: str = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "login" or "bind"
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )  # 仅 bind 场景：发起绑定的用户
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class OnboardingProgress(AuthBase):
    """注册后四步引导向导的分步持久化进度（每用户一行）。

    ``data`` 为以步骤号为 key 的分步合并数据，如 ``{1: {...}, 2: {...}}``。
    与前端 ``useOnboardingFlow`` 的 ``OnboardingState`` 契约对齐。
    """

    __tablename__: str = "onboarding_progress"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class User(UUIDPrimaryKeyMixin, AuthBase):
    __tablename__: str = "users"

    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    account_level: Mapped[str] = mapped_column(
        String(10), nullable=False, default="local"
    )
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    profile: Mapped[Profile] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user")
    oauth_bindings: Mapped[list[UserOAuth]] = relationship(back_populates="user")
    totp: Mapped[TOTP | None] = relationship(back_populates="user", uselist=False)
    recovery_codes: Mapped[list[RecoveryCode]] = relationship(back_populates="user")
    passkey_credentials: Mapped[list[PasskeyCredential]] = relationship(
        back_populates="user"
    )


class Profile(AuthBase):
    __tablename__: str = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), primary_key=True
    )
    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    avatar: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="profile")
