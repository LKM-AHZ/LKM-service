"""core 端口的 auth 侧实现：把 auth 的跨域能力绑定到 ``core.ports``。

**所有实现都惰性取 auth 内部属性**（``from auth import X as _x`` 放在方法体内）：
拆分前 app 侧测试大量 monkeypatch ``auth.seams.*`` / ``auth.user_dim_sync._session_factory``
/ ``auth.user_http.*`` 等；若这里在导入期写死引用，那些 patch 会静默失效、测试假绿。
"""

from __future__ import annotations

import uuid
from typing import Any


class _AuthzImpl:
    """鉴权与票据实现。"""

    def seam_enabled(self) -> bool:
        from auth import seams as _seams

        return bool(_seams.seam_enabled())

    def decode_access_token(self, token: str) -> Any:
        from auth.security import decode_access_token

        return decode_access_token(token)

    async def resolve_current_user(self, token: str, db: Any = None) -> Any:
        """裁决令牌对应的用户。

        ``db`` 可为 None（此时自开 auth 会话）——FastAPI 依赖路径与生产 WS 走这条；
        传入会话的场景是「调用方持有可查 users 的会话」（融合部署/测试）。
        """
        from auth import seams as _seams

        if db is not None:
            return await _seams.resolve_current_user(token, db)

        from auth.db.session import new_auth_session

        session = await new_auth_session()
        try:
            return await _seams.resolve_current_user(token, session)
        finally:
            await session.close()

    async def resolve_via_seam(
        self,
        user_id: uuid.UUID,
        expect_token_version: int,
        iat_ts: object,
        *,
        require_admin: bool,
        jti: str | None = None,
        selected_roles: tuple[str, ...] | None = None,
    ) -> Any:
        from auth import seams as _seams

        kwargs = {"selected_roles": selected_roles} if selected_roles is not None else {}
        return await _seams.resolve_via_seam(
            user_id,
            expect_token_version,
            iat_ts,
            require_admin=require_admin,
            jti=jti,
            **kwargs,
        )

    def create_admin_access_token(
        self, user: Any, mfa_verified: bool = False, mfa_at: int | None = None
    ) -> str:
        from auth.admin_session import create_admin_access_token

        return create_admin_access_token(user, mfa_verified=mfa_verified, mfa_at=mfa_at)

    def decode_admin_access(self, token: str) -> Any:
        from auth.admin_session import decode_admin_access

        return decode_admin_access(token)

    async def is_jti_blocked(self, jti: str | None) -> bool:
        from auth.token_revocation import is_jti_blocked

        return await is_jti_blocked(jti)


class _SnapshotImpl:
    """身份快照读实现。"""

    async def get_user_snapshot(self, db: Any, *, user_id: uuid.UUID) -> Any:
        from auth.snapshot import get_user_snapshot

        return await get_user_snapshot(db, user_id=user_id)

    async def get_user_snapshot_batch(
        self, db: Any, *, user_ids: list[uuid.UUID]
    ) -> Any:
        from auth.snapshot import get_user_snapshot_batch

        return await get_user_snapshot_batch(db, user_ids=user_ids)

    async def list_user_snapshots(
        self,
        db: Any,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
        include_pii: bool = False,
    ) -> Any:
        from auth.snapshot import list_user_snapshots

        return await list_user_snapshots(
            db, q=q, offset=offset, limit=limit, include_pii=include_pii
        )

    async def count_active_users(self, db: Any) -> int:
        from auth.snapshot import count_active_users

        return await count_active_users(db)

    async def user_count_by_day(self, db: Any, *, start: Any, days: int) -> Any:
        from auth.snapshot import user_count_by_day

        return await user_count_by_day(db, start=start, days=days)


class _AuditImpl:
    """审计写/导出与 auth 会话。"""

    async def log_audit(
        self,
        db: Any,
        user_id: uuid.UUID | None,
        action: str,
        detail: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        from auth.service_auth import log_audit

        await log_audit(db, user_id, action, detail=detail, ip_address=ip_address)

    async def export_audit_logs(self, db: Any, client: Any, *, window: int) -> int:
        from auth.audit_export import export_audit_logs

        return await export_audit_logs(db, client, window=window)

    async def new_auth_session(self) -> Any:
        from auth.db.session import new_auth_session

        return await new_auth_session()


class _UsersImpl:
    """渠道/provider、用户运维与 user_dim 对账。"""

    def get_channel(self, channel_key: str) -> Any:
        # seams.get_channel 是薄包装（每次重读 auth.channels.CHANNELS），patch 该表仍生效
        from auth import seams as _seams

        return _seams.get_channel(channel_key)

    def get_email_provider(self) -> Any:
        # 取 auth.deps 的模块属性（而非 seams 的导入期绑定），保住既有
        # `monkeypatch.setattr(auth.deps, "get_email_provider", ...)` 类打桩
        from auth import deps as _deps

        return _deps.get_email_provider()

    def get_sms_provider(self) -> Any:
        from auth import deps as _deps

        return _deps.get_sms_provider()

    async def ensure_demo_user(self, **kwargs: Any) -> Any:
        from auth import seams as _seams

        return await _seams.ensure_demo_user(**kwargs)

    async def mint_bot_sso_ticket(
        self, user_id: Any, account_level: str = "admin"
    ) -> dict[str, Any]:
        from auth import seams as _seams

        return await _seams.mint_bot_sso_ticket(
            user_id, account_level=account_level
        )

    async def verify_password(
        self, db: Any, username: str, password: str
    ) -> dict[str, Any] | None:
        """Basic 凭证校验：拆库走 HTTP 缝，融合形态就地查本库 User 行。

        融合分支原先写在 app 侧 git-http（那里直接 ``select(User)``），随端口化移入 auth——
        业务侧不该知道 users 表；``db`` 由调用方传入（融合形态下两边同库）。
        """
        from auth import seams as _seams

        if _seams.seam_enabled():
            return await _seams.verify_password_via_seam(username, password)

        from sqlalchemy import select

        from auth.entities import User

        user = (
            (await db.execute(select(User).where(User.username == username)))
            .scalars()
            .first()
        )
        if user is None or not user.hashed_password:
            return None
        if not await _seams.verifypwd(password, str(user.hashed_password)):
            return None
        return {"user_id": user.id, "username": user.username}

    async def verifypwd(self, plain: str, hashed: str) -> bool:
        from auth import seams as _seams

        return bool(await _seams.verifypwd(plain, hashed))

    async def hashpwd(self, plain: str) -> str:
        from auth import seams as _seams

        return str(await _seams.hashpwd(plain))

    async def open_session_pair(self) -> Any:
        from auth import seams as _seams

        return await _seams.open_session_pair()

    async def reconcile_user_dim_periodic(self) -> int:
        from auth import seams as _seams

        return await _seams.reconcile_user_dim_periodic()

    async def reconcile_user_dim_incremental(
        self, src: Any, tgt: Any, *, window: int
    ) -> int:
        from auth import seams as _seams

        return await _seams.reconcile_user_dim_incremental(src, tgt, window=window)

    async def sync_dim_for_ids(self, src: Any, tgt: Any, user_ids: list[int]) -> int:
        from auth import seams as _seams

        return await _seams.sync_dim_for_ids(src, tgt, user_ids)

    async def grant_exam_unlock_from_business(
        self,
        db: Any,
        user_id: uuid.UUID,
        *,
        unlock_level: str | None,
        unlock_role: str | None,
    ) -> None:
        from auth import seams as _seams

        await _seams.grant_exam_unlock_from_business(
            db, user_id, unlock_level=unlock_level, unlock_role=unlock_role
        )

    async def grant_incubation_from_business(
        self, db: Any, applicant_id: uuid.UUID
    ) -> None:
        from auth import seams as _seams

        await _seams.grant_incubation_from_business(db, applicant_id)


class _VerifyKeysImpl:
    """验签公钥与 passkey 挑战清理。"""

    async def refresh_verify_key(self) -> bool:
        from auth import seams as _seams

        return bool(await _seams.refresh_verify_key())

    def verify_key_status(self) -> Any:
        from auth import seams as _seams

        return _seams.verify_key_status()

    async def start_verify_key_refresh(self) -> None:
        from auth import seams as _seams

        await _seams.start_verify_key_refresh()

    async def stop_verify_key_refresh(self) -> None:
        from auth import seams as _seams

        await _seams.stop_verify_key_refresh()

    async def cleanup_expired_challenges(self) -> None:
        from auth import seams as _seams

        await _seams.cleanup_expired_challenges()


def _auth_session_dep() -> Any:
    """auth 会话的 FastAPI 依赖（core 侧 ``auth_session`` 包装它）。

    取模块属性而非导入期绑定，保住既有对 ``auth.db.session`` 的打桩。
    """
    from auth.db import session as _session

    return _session.get_auth_session


def bind_all() -> None:
    """把全部 auth 侧端口实现绑定进 core.ports（幂等）。"""
    from core import ports

    ports.install("authz", _AuthzImpl())
    ports.install("authz_session", _auth_session_dep())
    ports.install("snapshot", _SnapshotImpl())
    ports.install("audit", _AuditImpl())
    ports.install("users", _UsersImpl())
    ports.install("verify_keys", _VerifyKeysImpl())
