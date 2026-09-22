"""临时脚本：给指定 admin 启用 TOTP，并（需显式开启时）输出当前一次性验证码，供 step-up 链路验证。

**必须在 auth 容器内执行**（users/totp 真值在 `lkm_auth` 库，由 `LKM_AUTH_DB_*` 指定）。

用法： python scripts/setup_admin_2fa.py <username>
       LKM_ADMIN_2FA_DUMP=1 python scripts/setup_admin_2fa.py <username>   # 导出凭据

凭据（TOTP 密钥 / 当前验证码 / 恢复码）是管理员第二因子的等价物，默认**不输出**——
stdout 会被 shell history、CI 日志、终端回滚缓冲区留痕。显式设 ``LKM_ADMIN_2FA_DUMP=1``
时写入 ``.admin_2fa_dump``（0600，仅属主可读），用完请立即删除该文件与脚本本身。
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

from sqlalchemy import select

from auth.db.session import dispose_auth_engine, new_auth_session
from auth.entities import User
from auth.seams import setup_2fa_begin, setup_2fa_complete, totp_code, totp_now

_DUMP_ENV = "LKM_ADMIN_2FA_DUMP"
_DUMP_PATH = Path(".admin_2fa_dump")


def current_code(secret: str) -> str:
    key = base64.b32decode(secret, casefold=True)
    return totp_code(key, totp_now())


async def main() -> None:
    # 目标账号必须显式给：原先缺省到硬编码用户名，误跑一次（无参执行）就会真给那个账号开
    # 2FA；打印出的密钥一旦丢失，该账号就被锁在未知密钥+未知恢复码后面。
    if len(sys.argv) < 2:
        print("用法：python scripts/setup_admin_2fa.py <username>", file=sys.stderr)
        sys.exit(2)
    username = sys.argv[1]
    db = await new_auth_session()
    try:
        user = (
            (await db.execute(select(User).where(User.username == username)))
            .scalars()
            .first()
        )
        if user is None:
            print("[skip] 用户不存在")
            return
        # 两个 seam 都声明 -> dict[str, Any]，形状变化时不要让它以裸 KeyError 冒出：
        # 那时 DB 记录已被 setup_2fa_begin 改写过，KeyError 看不出「哪一步、期望什么键」。
        begin = await setup_2fa_begin(db, user.id)
        secret = begin.get("secret")
        if not isinstance(secret, str) or not secret:
            raise SystemExit(f"[fail] setup_2fa_begin 未返回 secret（keys={sorted(begin)}）")
        code = current_code(secret)
        res = await setup_2fa_complete(db, user.id, code)
        recovery_codes = res.get("recovery_codes")
        if not isinstance(recovery_codes, list) or not recovery_codes:
            # 此时尚未 commit：脚本自持会话、无自动提交，抛错后连接关闭即回滚，不会留下
            # 「2FA 开了但拿不到恢复码」的半成品状态。
            raise SystemExit(
                f"[fail] setup_2fa_complete 未返回 recovery_codes（keys={sorted(res)}）；"
                "本次设置未提交（已回滚），请排查后再跑"
            )
        await db.commit()

        if os.environ.get(_DUMP_ENV) != "1":
            print(
                f"[ok] 已为 {username} 启用 2FA；凭据未输出"
                f"（需导出请设 {_DUMP_ENV}=1）"
            )
            return
        # 0600：仅属主可读，避免写进日志/历史
        fd = os.open(_DUMP_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"SECRET={secret}\n")
            f.write(f"CURRENT_CODE={code}\n")
            f.write(f"RECOVERY={','.join(recovery_codes)}\n")
        print(f"[ok] 已为 {username} 启用 2FA；凭据写入 {_DUMP_PATH}（0600），用完请删除")
    finally:
        await db.close()
        await dispose_auth_engine()


if __name__ == "__main__":
    asyncio.run(main())
