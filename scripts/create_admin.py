"""一次性初始化脚本：创建管理员账户。

**必须在 auth 容器内执行**（用户/档案真值在 `lkm_auth` 库，由 `LKM_AUTH_DB_*` 指定；
backend 容器只有 `LKM_DB_*` 业务库，在那边跑会插错库或直接失败）：

    LKM_ADMIN_PASSWORD='密码' docker compose exec -e LKM_ADMIN_PASSWORD auth \
        python scripts/create_admin.py almauser 'email' 'phone'

    # 也可省略密码改为交互输入（getpass）
    docker compose exec -it auth python scripts/create_admin.py almauser 'email' 'phone'

密码复用 auth 域的标准哈希与新会话工厂。日常建号更推荐 ``scripts/admin_ops.py create``
（支持幂等提示、避免命令行明文密码）。
"""

import asyncio
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.db.session import dispose_auth_engine, new_auth_session
from auth.entities import Profile, User
from auth.seams import hashpwd, verifypwd

_USAGE = (
    "用法：python scripts/create_admin.py <用户名> <邮箱> <手机> [密码]\n"
    "  密码建议省略，改用 LKM_ADMIN_PASSWORD 环境变量或交互输入"
    "（命令行明文会进 shell history，且执行期间 ps/proc 可见）"
)


def _resolve_password(cli_value: str | None) -> str:
    """取建号密码：显式位置参数（兼容旧用法，告警）> LKM_ADMIN_PASSWORD > 交互输入。"""
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


async def main() -> None:
    # 先校验参数个数：原先直接取 argv[1..4]，少参会抛裸 IndexError，多参会被静默忽略
    if not 4 <= len(sys.argv) <= 5:
        print(_USAGE, file=sys.stderr)
        sys.exit(2)
    username, email, phone = sys.argv[1], sys.argv[2], sys.argv[3]
    raw_password = _resolve_password(sys.argv[4] if len(sys.argv) > 4 else None)

    db = await new_auth_session()
    try:
        # 幂等：已存在则提示退出，避免唯一约束冲突。
        # 三列各自唯一，OR 可能同时命中**不同**用户，故取全部命中行逐个提示，
        # 不能用 scalar_one_or_none（多命中会抛 MultipleResultsFound 崩成 traceback）。
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
            return

        hashed = await hashpwd(raw_password)
        user = User(
            username=username,
            email=email,
            phone=phone,
            hashed_password=hashed,
            account_level="admin",
        )
        profile = Profile(user=user, nickname=username, role="admin")
        db.add(user)
        db.add(profile)
        try:
            await db.commit()
        except IntegrityError:
            # 先查后插非原子：并发/重复执行时唯一约束兜底，转成友好提示而不是栈。
            await db.rollback()
            print(f"[skip] 唯一约束冲突（并发或重复执行）：{username}")
            return
        await db.refresh(user)
        print(
            f"[ok] 管理员创建成功: id={user.id} username={user.username} account_level={user.account_level}"
        )

        # 验证登录链路：哈希可校验。用显式 raise 而非 assert——assert 在 python -O 下会被
        # 整条剥掉，而这是新凭据的唯一自检，静默跳过等于放行一个登不上的管理员账号。
        if not await verifypwd(raw_password, user.hashed_password):
            raise SystemExit(
                "[fail] 密码哈希校验失败（哈希链路异常），请勿使用该账号并排查 hashpwd/verifypwd"
            )
        print("[ok] 密码哈希校验通过")
    finally:
        await db.close()
        await dispose_auth_engine()


if __name__ == "__main__":
    asyncio.run(main())
