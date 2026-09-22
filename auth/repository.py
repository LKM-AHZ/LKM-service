"""auth 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**auth 域的领域查询/语句构造**。service 层因此不再 ``import sqlalchemy``。

分工口径（批 3）：

- ``app.db.repo`` 的 ``get_or_raise`` / ``consume_once`` / ``isolated_update`` 仍是
  三把独立原语，由 service 直接调用；本模块只提供它们需要的**条件元组**或
  ``UPDATE`` 语句（如 :meth:`RefreshTokenRepository.consume_conditions`、
  :meth:`UserRepository.failed_login_stmt`），不改其语义。
- 唯一例外是 :func:`is_integrity_error` / :func:`is_operational_error`：把
  SQLAlchemy 异常类型判定收在此处，供 service 的 ``except`` 归类（避免 service
  直接 import ``sqlalchemy.exc``），判定结果与直接 ``isinstance`` 完全一致。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Update, case, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from app.core.err import ErrCode
from app.db.base import expires_at
from app.db.repository import AsyncRepository, DbSession
from auth.models import (
    TOTP,
    AuditLog,
    MagicLink,
    OAuthState,
    OnboardingProgress,
    PasskeyChallenge,
    PasskeyCredential,
    PendingRegistration,
    Profile,
    RecoveryCode,
    RecoveryTransaction,
    RefreshToken,
    TempTokenUsage,
    User,
    UserOAuth,
)


def is_integrity_error(exc: BaseException) -> bool:
    """是否为 SQLAlchemy ``IntegrityError``（唯一约束等）。"""
    return isinstance(exc, IntegrityError)


def is_operational_error(exc: BaseException) -> bool:
    """是否为 SQLAlchemy ``OperationalError``（死锁/连接等运行期故障）。"""
    return isinstance(exc, OperationalError)


class UserRepository(AsyncRepository[User]):
    model = User

    async def get_with_profile(self, user_id: uuid.UUID) -> User | None:
        """按主键取用户并预载 ``profile``（异步下禁止 lazy load）。"""
        return await self.get(user_id, options=(selectinload(User.profile),))

    async def get_with_profile_or_raise(
        self,
        user_id: uuid.UUID,
        errcode: ErrCode,
        *,
        detail: str | None = None,
    ) -> User:
        return await self.get_or_raise(
            user_id, errcode, detail=detail, options=(selectinload(User.profile),)
        )

    async def get_by_username(self, username: str) -> User | None:
        return await self.get_one(User.username == username)

    async def get_by_username_with_profile(self, username: str) -> User | None:
        return await self.get_one(
            User.username == username, options=(selectinload(User.profile),)
        )

    async def get_by_username_or_raise(
        self, username: str, errcode: ErrCode, *, detail: str | None = None
    ) -> User:
        return await self.get_one_or_raise(
            errcode, User.username == username, detail=detail
        )

    async def get_by_email(self, email: str) -> User | None:
        return await self.get_one(User.email == email)

    async def get_by_email_or_raise(
        self, email: str, errcode: ErrCode, *, detail: str | None = None
    ) -> User:
        return await self.get_one_or_raise(
            errcode,
            User.email == email,
            detail=detail,
            options=(selectinload(User.profile),),
        )

    async def find_for_login(
        self, *, username: str, email: str, phone: str
    ) -> User | None:
        """按用户名 / 邮箱 / 手机号任一命中取 User（登录入口，预载 profile）。

        空联系方式不计入谓词（同 :meth:`find_for_registration`）：``User.email == None``
        会编译成 ``email IS NULL``，把「没填邮箱」的任意用户当成命中，登录路径上即身份错配。
        """
        return await self.get_one(
            or_(
                User.username == username,
                (User.email == email) if email else False,
                (User.phone == phone) if phone else False,
            ),
            options=(selectinload(User.profile),),
        )

    async def find_by_email_or_phone(self, contact: str) -> User | None:
        if not contact:
            return None
        return await self.get_one(or_(User.email == contact, User.phone == contact))

    async def find_for_registration(
        self, *, username: str, email: str | None, phone: str | None
    ) -> User | None:
        """注册查重：用户名 / 邮箱 / 手机号任一命中（空联系方式不计入谓词）。"""
        return await self.get_one(
            or_(
                User.username == username,
                (User.email == email) if email else False,
                (User.phone == phone) if phone else False,
            ),
            options=(selectinload(User.profile),),
        )

    async def username_exists(self, username: str) -> bool:
        return await self.exists(User.username == username)

    async def get_account_level(self, user_id: uuid.UUID) -> str | None:
        """只取 ``account_level`` 列（未命中返回 ``None``）。"""
        return await self.db.scalar(
            select(User.account_level).where(User.id == user_id)
        )

    async def get_level_and_role(
        self, user_id: uuid.UUID
    ) -> tuple[str, str | None] | None:
        """取 ``(account_level, Profile.role)``；用户不存在返回 ``None``（外连接）。"""
        row = (
            await self.db.execute(
                select(User.account_level, Profile.role)
                .outerjoin(Profile, Profile.user_id == User.id)
                .where(User.id == user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    async def set_account_level(self, user_id: uuid.UUID, level: str) -> int:
        return await self.update_where({"account_level": level}, User.id == user_id)

    async def bump_token_version(self, user_id: uuid.UUID) -> int:
        """递增 ``token_version``，使该用户已签发的访问令牌全部失效。"""
        return await self.update_where(
            {"token_version": User.token_version + 1}, User.id == user_id
        )

    def failed_login_stmt(
        self, user_id: uuid.UUID, *, threshold: int, lock_minutes: int
    ) -> Update:
        """登录失败计数 + 同语句原子锁定，供 ``isolated_update`` 在 savepoint 内执行。

        自增与锁定判定合并为单条 UPDATE：同一语句内所有列引用都取旧行值，
        由数据库对该行加锁，避免「自增后 refresh 再比较」在并发下的漏锁竞态。
        """
        return (
            sa_update(User)
            .where(User.id == user_id)
            .values(
                failed_login_attempts=User.failed_login_attempts + 1,
                is_locked=User.failed_login_attempts >= threshold - 1,
                locked_until=case(
                    (
                        User.failed_login_attempts >= threshold - 1,
                        expires_at(minutes=lock_minutes),
                    ),
                    else_=User.locked_until,
                ),
            )
        )

    def reset_login_failures_stmt(self, user_id: uuid.UUID) -> Update:
        """成功登录后清零失败计数/解锁，供 ``isolated_update`` 在 savepoint 内执行。"""
        return (
            sa_update(User)
            .where(User.id == user_id)
            .values(failed_login_attempts=0, is_locked=False, locked_until=None)
        )


class ProfileRepository(AsyncRepository[Profile]):
    model = Profile
    # Profile 主键列名是 user_id，非基类默认的 id。
    pk_attr = "user_id"

    async def set_role(self, user_id: uuid.UUID, role: str) -> int:
        return await self.update_where({"role": role}, Profile.user_id == user_id)


class RefreshTokenRepository(AsyncRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_hash_or_raise(
        self, token_hash: str, errcode: ErrCode, *, detail: str | None = None
    ) -> RefreshToken:
        return await self.get_one_or_raise(
            errcode, RefreshToken.token_hash == token_hash, detail=detail
        )

    def consume_conditions(self, token_hash: str) -> tuple[Any, ...]:
        """原子撤销 web 端点刷新令牌的谓词（``kind == "web"`` 堵跨会话互用）。"""
        return (
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.kind == "web",
        )

    async def revoke_all_for_user(
        self, user_id: uuid.UUID, now: datetime.datetime
    ) -> int:
        return await self.update_where(
            {"revoked_at": now},
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )


class MagicLinkRepository(AsyncRepository[MagicLink]):
    model = MagicLink

    async def get_by_hash(self, token_hash: str) -> MagicLink | None:
        return await self.get_one(MagicLink.token_hash == token_hash)

    async def get_by_hash_or_raise(
        self, token_hash: str, errcode: ErrCode, *, detail: str | None = None
    ) -> MagicLink:
        return await self.get_one_or_raise(
            errcode, MagicLink.token_hash == token_hash, detail=detail
        )

    def consume_conditions(
        self, token_hash: str, purpose: str, now: datetime.datetime
    ) -> tuple[Any, ...]:
        return (
            MagicLink.token_hash == token_hash,
            MagicLink.used.is_(False),
            MagicLink.purpose == purpose,
            MagicLink.expires_at > now,
        )


class OAuthStateRepository(AsyncRepository[OAuthState]):
    model = OAuthState

    async def get_by_state_or_raise(
        self, state: str, errcode: ErrCode, *, detail: str | None = None
    ) -> OAuthState:
        return await self.get_one_or_raise(
            errcode, OAuthState.state == state, detail=detail
        )

    def consume_conditions(
        self, state: str, purpose: str, now: datetime.datetime
    ) -> tuple[Any, ...]:
        return (
            OAuthState.state == state,
            OAuthState.consumed.is_(False),
            OAuthState.purpose == purpose,
            OAuthState.expires_at > now,
        )


class UserOAuthRepository(AsyncRepository[UserOAuth]):
    model = UserOAuth

    async def find_by_provider_user(
        self, provider: str, provider_user_id: str
    ) -> UserOAuth | None:
        return await self.get_one(
            UserOAuth.provider == provider,
            UserOAuth.provider_user_id == provider_user_id,
        )


class TOTPRepository(AsyncRepository[TOTP]):
    model = TOTP
    # TOTP 主键列名是 user_id。
    pk_attr = "user_id"

    async def get_by_user(self, user_id: uuid.UUID) -> TOTP | None:
        return await self.get(user_id)

    async def get_enabled(self, user_id: uuid.UUID) -> TOTP | None:
        return await self.get_one(TOTP.user_id == user_id, TOTP.enabled.is_(True))

    def failure_stmt(self, user_id: uuid.UUID) -> Update:
        """TOTP 失败计数 +1，供 ``isolated_update`` 在 savepoint 内执行。"""
        return (
            sa_update(TOTP)
            .where(TOTP.user_id == user_id)
            .values(failed_attempts=TOTP.failed_attempts + 1)
        )

    def replay_conditions(self, user_id: uuid.UUID, counter: int) -> tuple[Any, ...]:
        """TOTP 重放保护谓词：仅当计数器尚未记录/更小时才允许原子消费。"""
        return (
            TOTP.user_id == user_id,
            or_(TOTP.last_counter.is_(None), TOTP.last_counter < counter),
        )


class RecoveryCodeRepository(AsyncRepository[RecoveryCode]):
    model = RecoveryCode

    async def max_unused_failed_attempts(self, user_id: uuid.UUID) -> int | None:
        """未使用恢复码中的最大失败计数（暴力尝试锁定判定）。"""
        return await self.db.scalar(
            select(func.max(RecoveryCode.failed_attempts)).where(
                RecoveryCode.user_id == user_id, RecoveryCode.used.is_(False)
            )
        )

    def consume_conditions(
        self, user_id: uuid.UUID, code_hashes: list[str]
    ) -> tuple[Any, ...]:
        return (
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash.in_(code_hashes),
            RecoveryCode.used.is_(False),
        )

    def failure_stmt(self, user_id: uuid.UUID) -> Update:
        """恢复码失败计数 +1（仅未用码），供 ``isolated_update`` 在 savepoint 内执行。"""
        return (
            sa_update(RecoveryCode)
            .where(RecoveryCode.user_id == user_id, RecoveryCode.used.is_(False))
            .values(failed_attempts=RecoveryCode.failed_attempts + 1)
        )

    async def reset_failures(self, user_id: uuid.UUID) -> int:
        return await self.update_where(
            {"failed_attempts": 0},
            RecoveryCode.user_id == user_id,
            RecoveryCode.used.is_(False),
        )

    async def delete_for_user(self, user_id: uuid.UUID) -> int:
        """关闭 2FA：抹掉该用户全部恢复码（硬删）。"""
        return await self.hard_delete_where(RecoveryCode.user_id == user_id)


class TempTokenUsageRepository(AsyncRepository[TempTokenUsage]):
    model = TempTokenUsage

    async def get_by_hash(self, token_hash: str) -> TempTokenUsage | None:
        return await self.get_one(TempTokenUsage.token_hash == token_hash)

    async def find_recovery_usage(
        self, *, token_hash: str, user_id: uuid.UUID, txn_id: str
    ) -> TempTokenUsage | None:
        """恢复流程核验：临时令牌须已被 2FA 消费且归属本事务。"""
        return await self.get_one(
            TempTokenUsage.token_hash == token_hash,
            TempTokenUsage.user_id == user_id,
            TempTokenUsage.purpose == "recovery",
            TempTokenUsage.txn_id == txn_id,
            TempTokenUsage.consumed.is_(True),
        )


class PasskeyCredentialRepository(AsyncRepository[PasskeyCredential]):
    model = PasskeyCredential

    async def list_for_user(self, user_id: uuid.UUID) -> list[PasskeyCredential]:
        return await self.get_many(PasskeyCredential.user_id == user_id)

    async def find_by_credential_id(
        self, credential_id: str
    ) -> PasskeyCredential | None:
        return await self.get_one(PasskeyCredential.credential_id == credential_id)

    async def get_by_credential_id_or_raise(
        self, credential_id: str, errcode: ErrCode, *, detail: str | None = None
    ) -> PasskeyCredential:
        return await self.get_one_or_raise(
            errcode,
            PasskeyCredential.credential_id == credential_id,
            detail=detail,
        )

    async def get_for_user_or_raise(
        self,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        errcode: ErrCode,
        *,
        detail: str | None = None,
    ) -> PasskeyCredential:
        return await self.get_one_or_raise(
            errcode,
            PasskeyCredential.id == credential_id,
            PasskeyCredential.user_id == user_id,
            detail=detail,
        )


class PasskeyChallengeRepository(AsyncRepository[PasskeyChallenge]):
    model = PasskeyChallenge

    def consume_conditions(
        self, challenge_id: str, now: datetime.datetime
    ) -> tuple[Any, ...]:
        return (
            PasskeyChallenge.challenge_id == challenge_id,
            PasskeyChallenge.consumed.is_(False),
            PasskeyChallenge.expires_at > now,
        )

    async def get_by_challenge_id(self, challenge_id: str) -> PasskeyChallenge | None:
        return await self.get_one(PasskeyChallenge.challenge_id == challenge_id)

    async def delete_expired_or_consumed(self, now: datetime.datetime) -> int:
        """后台清理：删掉已消费或已过期的挑战码，返回删除行数。"""
        return await self.hard_delete_where(
            or_(
                PasskeyChallenge.consumed.is_(True),
                PasskeyChallenge.expires_at <= now,
            )
        )


class PendingRegistrationRepository(AsyncRepository[PendingRegistration]):
    model = PendingRegistration

    async def get_by_txn_or_raise(
        self, txn_id: str, errcode: ErrCode, *, detail: str | None = None
    ) -> PendingRegistration:
        return await self.get_one_or_raise(
            errcode, PendingRegistration.txn_id == txn_id, detail=detail
        )


class RecoveryTransactionRepository(AsyncRepository[RecoveryTransaction]):
    model = RecoveryTransaction

    async def get_by_txn_or_raise(
        self, txn_id: str, errcode: ErrCode, *, detail: str | None = None
    ) -> RecoveryTransaction:
        return await self.get_one_or_raise(
            errcode, RecoveryTransaction.txn_id == txn_id, detail=detail
        )

    def consume_conditions(
        self, txn_id: str, now: datetime.datetime
    ) -> tuple[Any, ...]:
        """原子消费恢复事务的谓词（须联系方式 + 第二因素均已验且未过期）。"""
        return (
            RecoveryTransaction.txn_id == txn_id,
            RecoveryTransaction.consumed.is_(False),
            RecoveryTransaction.contact_verified.is_(True),
            RecoveryTransaction.totp_verified.is_(True),
            RecoveryTransaction.expires_at > now,
        )


class OnboardingProgressRepository(AsyncRepository[OnboardingProgress]):
    model = OnboardingProgress
    # OnboardingProgress 主键列名是 user_id。
    pk_attr = "user_id"

    async def get_by_user(self, user_id: uuid.UUID) -> OnboardingProgress | None:
        return await self.get(user_id)


class AuditLogRepository(AsyncRepository[AuditLog]):
    model = AuditLog


class VerificationRepository(AsyncRepository[Any]):
    """邮箱/手机验证码记录的通用仓储 —— 两个模型字段同名同构，故按模型参数化。

    ``contact_attr`` 仅在 :meth:`latest` 中用到（``email`` 或 ``phone``）；
    创建/失败计数不依赖它，可省略。
    """

    def __init__(self, db: DbSession, model: type[Any], contact_attr: str = "") -> None:
        super().__init__(db)
        self.model = model
        self.contact_attr = contact_attr

    async def latest(self, contact: str, purpose: str) -> Any:
        """该联系方式未使用的最新一条验证码记录。"""
        contact_column = getattr(self.model, self.contact_attr)
        return await self.get_one(
            contact_column == contact,
            self.model.purpose == purpose,
            self.model.used.is_(False),
            order_by=self.model.created_at.desc(),
        )

    def failed_stmt(self, pk: Any) -> Update:
        """验证码失败计数 +1，供 ``isolated_update`` 在 savepoint 内执行。"""
        return (
            sa_update(self.model)
            .where(self.pk_column == pk)
            .values(failed_attempts=self.model.failed_attempts + 1)
        )


__all__ = [
    "AuditLogRepository",
    "MagicLinkRepository",
    "OAuthStateRepository",
    "OnboardingProgressRepository",
    "PasskeyChallengeRepository",
    "PasskeyCredentialRepository",
    "PendingRegistrationRepository",
    "ProfileRepository",
    "RecoveryCodeRepository",
    "RecoveryTransactionRepository",
    "RefreshTokenRepository",
    "TOTPRepository",
    "TempTokenUsageRepository",
    "UserOAuthRepository",
    "UserRepository",
    "VerificationRepository",
    "is_integrity_error",
    "is_operational_error",
]
