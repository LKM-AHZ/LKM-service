"""管理员与会话运维（auth 库真值）。

**必须在 auth 容器内执行**：拆库后管理员真值（users / profiles / refresh_tokens / totp）
在 `lkm_auth` 库，只有 auth 服务配了 `LKM_AUTH_DB_*`。backend 容器只有 `LKM_DB_*`（biz 库），
在那边跑本脚本会连错库。

    docker compose exec auth python scripts/admin_ops.py list
    docker compose exec auth python scripts/admin_ops.py unlock alma
    docker compose exec auth python scripts/admin_ops.py revoke alma
    docker compose exec auth python scripts/admin_ops.py reset-2fa alma --yes
    docker compose exec auth python scripts/admin_ops.py create alma e@x.com 13800000000 '密码'

取代 OPS-CHEATSHEET.md 里那段手写 heredoc 建号命令（后者用主库会话，拆库后已失效）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.auth_session import dispose_auth_engine, new_auth_session
from app.db.base import now_iso
from app.modules.auth.models import TOTP, Profile, RecoveryCode, RefreshToken, User
from app.modules.auth.security import hashpwd


async def _find(db: AsyncSession, username: str) -> User | None:
    return (
        (
            await db.execute(
                select(User)
                .where(User.username == username)
                .options(selectinload(User.totp))
            )
        )
        .scalars()
        .first()
    )


async def cmd_list() -> int:
    db = await new_auth_session()
    try:
        users = (
            (
                await db.execute(
                    select(User)
                    .where(User.account_level == "admin")
                    .options(selectinload(User.totp))
                    .order_by(User.username)
                )
            )
            .scalars()
            .all()
        )
        if not users:
            print("无管理员账户。用 create 子命令建号。")
            return 0
        print(f"{'username':<18} {'locked':<7} {'tok_ver':<8} {'totp':<6} id")
        for u in users:
            totp = "on" if (u.totp and u.totp.enabled) else "-"
            print(
                f"{u.username:<18} {u.is_locked!s:<7} {u.token_version:<8} {totp:<6} {u.id}"
            )
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


async def cmd_unlock(username: str) -> int:
    db = await new_auth_session()
    try:
        user = await _find(db, username)
        if user is None:
            print(f"[skip] 用户不存在：{username}")
            return 1
        user.is_locked = False
        user.locked_until = None
        user.failed_login_attempts = 0
        await db.commit()
        print(f"[ok] 已解锁 {username}")
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


async def cmd_revoke(username: str) -> int:
    """吊销该用户**全部**会话（前台 + 后台）。

    token_version++ 使已签发的 access token 立即失效（admin 侧还叠加 updated_at 改密撤销
    判定），同时把未撤销的 refresh token 全部标记 revoked。
    """
    db = await new_auth_session()
    try:
        user = await _find(db, username)
        if user is None:
            print(f"[skip] 用户不存在：{username}")
            return 1
        user.token_version += 1
        result = await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now_iso())
        )
        await db.commit()
        print(
            f"[ok] 已吊销 {username} 的全部会话（token_version→{user.token_version}，"
            f"refresh 撤销 {result.rowcount} 条）"
        )
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


async def cmd_reset_2fa(username: str) -> int:
    db = await new_auth_session()
    try:
        user = await _find(db, username)
        if user is None:
            print(f"[skip] 用户不存在：{username}")
            return 1
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
        await db.execute(delete(TOTP).where(TOTP.user_id == user.id))
        await db.commit()
        print(f"[ok] 已重置 {username} 的 2FA（TOTP + 恢复码）；下次危险操作需重新绑定")
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


async def cmd_create(username: str, email: str, phone: str, password: str) -> int:
    db = await new_auth_session()
    try:
        existing = (
            await db.execute(
                select(User).where(
                    (User.username == username)
                    | (User.email == email)
                    | (User.phone == phone)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"[skip] 已存在：username={existing.username} id={existing.id}")
            return 1
        user = User(
            username=username,
            email=email,
            phone=phone,
            hashed_password=await hashpwd(password),
            account_level="admin",
        )
        db.add(user)
        db.add(Profile(user=user, nickname=username, role="admin"))
        await db.commit()
        await db.refresh(user)
        print(f"[ok] 管理员创建成功：id={user.id} username={user.username}")
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="管理员与会话运维（auth 库）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部管理员").set_defaults(
        run=lambda _a: cmd_list()
    )

    p_unlock = sub.add_parser("unlock", help="解锁账号并清失败计数")
    p_unlock.add_argument("username")
    p_unlock.set_defaults(run=lambda a: cmd_unlock(a.username))

    p_revoke = sub.add_parser("revoke", help="吊销该用户全部会话（前台+后台）")
    p_revoke.add_argument("username")
    p_revoke.set_defaults(run=lambda a: cmd_revoke(a.username))

    p_reset = sub.add_parser("reset-2fa", help="重置 TOTP 与恢复码（破坏性）")
    p_reset.add_argument("username")
    p_reset.add_argument("--yes", action="store_true", help="确认破坏性操作")
    p_reset.set_defaults(run=None)

    p_create = sub.add_parser("create", help="创建管理员（幂等）")
    p_create.add_argument("username")
    p_create.add_argument("email")
    p_create.add_argument("phone")
    p_create.add_argument("password")
    p_create.set_defaults(run=lambda a: cmd_create(a.username, a.email, a.phone, a.password))

    args = parser.parse_args()

    if args.cmd == "reset-2fa":
        if not args.yes:
            print(
                f"⚠ 即将删除 {args.username} 的 TOTP 与恢复码。确认请加 --yes。",
                file=sys.stderr,
            )
            return 1
        return asyncio.run(cmd_reset_2fa(args.username))

    return asyncio.run(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
