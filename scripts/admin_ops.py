"""管理员与会话运维（auth 库真值）。

**必须在 auth 容器内执行**：拆库后管理员真值（users / profiles / refresh_tokens / totp）
在 `lkm_auth` 库，只有 auth 服务配了 `LKM_AUTH_DB_*`。backend 容器只有 `LKM_DB_*`（biz 库），
在那边跑本脚本会连错库。

    docker compose exec auth python scripts/admin_ops.py list
    docker compose exec auth python scripts/admin_ops.py unlock alma
    docker compose exec auth python scripts/admin_ops.py revoke alma
    docker compose exec auth python scripts/admin_ops.py reset-2fa alma --yes
    LKM_ADMIN_PASSWORD='密码' docker compose exec -e LKM_ADMIN_PASSWORD auth \
        python scripts/admin_ops.py create alma e@x.com 13800000000

    # 也可省略密码改为交互输入（getpass）：
    docker compose exec -it auth python scripts/admin_ops.py create alma e@x.com 13800000000

取代 OPS-CHEATSHEET.md 里那段手写 heredoc 建号命令（后者用主库会话，拆库后已失效）。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import now_iso
from auth.db.session import dispose_auth_engine, new_auth_session
from auth.entities import TOTP, Profile, RecoveryCode, RefreshToken, User
from auth.schemas import Password
from auth.seams import hashpwd

# 与 API 侧同一个密码策略类型（auth/schemas.Password）：脚本建号也必须过同一道校验，
# 否则运维能直接建出 `1` 这种弱口令管理员，绕开注册/改密端点的约束。
_PASSWORD_ADAPTER: TypeAdapter[str] = TypeAdapter(Password)


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


async def _revoke_sessions(db: AsyncSession, user: User) -> int:
    """吊销该用户全部会话：``token_version++``（作废已签发 access）+ 批量撤销 refresh。

    调用方负责 commit。返回被撤销的 refresh 条数。
    """
    user.token_version += 1
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now_iso())
    )
    return int(result.rowcount or 0)


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
        revoked = await _revoke_sessions(db, user)
        await db.commit()
        print(
            f"[ok] 已吊销 {username} 的全部会话（token_version→{user.token_version}，"
            f"refresh 撤销 {revoked} 条）"
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
        # 同时吊销全部会话：重置 2FA 的典型场景是「账户可能已被他人登录控制」，
        # 不吊销的话攻击者手里的 access/refresh 仍然有效，重置等于没做。
        revoked = await _revoke_sessions(db, user)
        await db.commit()
        print(
            f"[ok] 已重置 {username} 的 2FA（TOTP + 恢复码），并吊销其全部会话"
            f"（token_version→{user.token_version}，refresh 撤销 {revoked} 条）；"
            f"下次危险操作需重新绑定"
        )
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


async def cmd_create(username: str, email: str, phone: str, password: str) -> int:
    db = await new_auth_session()
    try:
        # 三列各自唯一，OR 可能同时命中**不同**用户：取全部命中行逐个提示，
        # 不能用 scalar_one_or_none（两个命中会抛 MultipleResultsFound 崩成 traceback）。
        existing = (
            (
                await db.execute(
                    select(User).where(
                        (User.username == username)
                        | (User.email == email)
                        | (User.phone == phone)
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            detail = ", ".join(f"{u.username}({u.id})" for u in existing)
            print(f"[skip] 已存在同用户名/邮箱/手机号的账号：{detail}")
            return 1
        try:
            _PASSWORD_ADAPTER.validate_python(password)
        except ValidationError as exc:
            print(
                f"[skip] 密码不符合策略（{exc.errors()[0]['msg']}）："
                "至少 6 位，与 API 侧 auth/schemas._validate_password 同规",
                file=sys.stderr,
            )
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
        try:
            await db.commit()
        except IntegrityError:
            # 先查后插非原子：并发/重复执行时唯一约束兜底，转成友好提示而不是栈。
            await db.rollback()
            print(f"[skip] 唯一约束冲突（并发或重复执行）：{username}")
            return 1
        await db.refresh(user)
        print(f"[ok] 管理员创建成功：id={user.id} username={user.username}")
        return 0
    finally:
        await db.close()
        await dispose_auth_engine()


def _resolve_password(cli_value: str | None) -> str:
    """取待建管理员的密码：显式位置参数 > ``LKM_ADMIN_PASSWORD`` > 交互式输入。

    位置参数仅为兼容既有的 `admin_ops.py create <用户名> <邮箱> <手机> <密码>` 用法保留，
    但明文会留在 shell history 与进程 cmdline（容器内任意进程可经 ps/proc 读到），故告警提示。
    """
    if cli_value:
        print(
            "⚠ 检测到明文密码位置参数：它会留在 shell history，且执行期间可被 ps/proc 读到；"
            "建议改用 LKM_ADMIN_PASSWORD 或不传该参数以交互输入。",
            file=sys.stderr,
        )
        return cli_value
    env_pw = os.environ.get("LKM_ADMIN_PASSWORD")
    if env_pw:
        return env_pw
    return getpass.getpass("管理员密码: ")


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
    p_create.add_argument(
        "password",
        nargs="?",
        default=None,
        help="可选，强烈建议省略：改用 LKM_ADMIN_PASSWORD 环境变量或交互输入"
        "（命令行明文会进 shell history，且执行期间 ps/proc 可见）",
    )
    p_create.set_defaults(
        run=lambda a: cmd_create(
            a.username, a.email, a.phone, _resolve_password(a.password)
        )
    )

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
