"""一次性初始化脚本：创建管理员账户。

用法（后端容器内）：
    python scripts/create_admin.py almauser 'email' 'phone' 'password'

从环境变量(LKM_DB_*)读取连接配置，复用 app 的标准密码哈希与新会话工厂。
"""

import asyncio
import sys

from sqlalchemy import select

from app.db.session import dispose_engine, get_async_engine, new_session
from auth.entities import Profile, User
from auth.seams import hashpwd


async def main() -> None:
    username, email, phone, raw_password = (
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        sys.argv[4],
    )

    get_async_engine()
    db = await new_session()
    try:
        # 幂等：已存在则提示退出，避免唯一约束冲突
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
            print(f"[skip] 用户已存在: username={existing.username} id={existing.id}")
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
        await db.commit()
        await db.refresh(user)
        print(
            f"[ok] 管理员创建成功: id={user.id} username={user.username} account_level={user.account_level}"
        )

        # 验证登录链路：哈希可校验
        from auth.seams import verifypwd

        assert await verifypwd(raw_password, user.hashed_password), "密码校验异常"
        print("[ok] 密码哈希校验通过")
    finally:
        await db.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
