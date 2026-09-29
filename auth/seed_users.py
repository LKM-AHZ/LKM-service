"""演示用户（seed 专用）：app 侧示例数据脚本的跨 realm 造用户入口。

**为什么必须有这个模块**：`app/modules/**/seed.py`（columns / files 两个）需要「一个真实存在于
auth realm 的演示作者/上传者」——业务行 `columns.owner_id` / `library_files.uploader_id` 是
**裸 uuid 指向 auth realm 用户**（跨库无 FK，见 `tests/test_boards_columns.py` 头注），前端
再经 `user:snap` + AUTH 回退把 id 解析成昵称。

**为什么不能由 app 侧自己写**：用户表唯属 auth（蓝图 §3.1 单向权威边界「主服务绝不直接修改任何
AUTH 数据」）。原先两个 seed 用**业务库会话** `db.add(User(...))` 直接写 users/profiles——在融合
单库下侥幸能跑，物理拆库后业务库没有 users 表，必 `UndefinedTable`（compose 自注「S5 拆库后
users/profiles 只在 auth 独立库，业务库无 users 表」）。测试也因此绕开了实体 seed 的副作用。

**语义**：以 auth 库会话 upsert 一个**不可登录**的演示用户（口令是哨兵串、不是任何真实口令的
合法 Argon2 哈希），幂等；返回其 uuid 供业务行引用。写操作落在 auth 域（auth 是用户数据唯一入口），
app 侧只经 ``auth.seams.ensure_demo_user`` 调用这一接口——与
``grant_incubation_from_business`` / ``reconcile_user_dim_periodic`` 同为「app 调 auth 自有能力」
的范式。

会话工厂刻意做成模块级可替换（同 ``auth.user_dim_sync._session_factory``），便于测试把 seed 的
auth 会话指到测试专属 auth 库。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import new_auth_session
from auth.models import Profile, User

# 演示用户一律不可登录：口令位是哨兵串，绝非任何真实口令的合法哈希。同时它与
# `_SEED_AUTHOR_USERNAME` 同名标识出「这是 seed 造的」，运维可据此识别/清理。
_SEED_PASSWORD_SENTINEL = "!seed-only-no-login"

_session_factory: Callable[[], Awaitable[AsyncSession]] = new_auth_session


async def ensure_demo_user(
    *,
    username: str,
    nickname: str,
    email: str | None = None,
    account_level: str = "local",
) -> uuid.UUID:
    """幂等确保演示用户存在，返回其 uuid（User + Profile 齐全）。

    自开 auth 库会话并**独立提交**（不依托任何业务事务）；重复调用不重复插入。演示用户的
    ``hashed_password`` 为哨兵值 → 无法登录，仅用于让业务行的裸 uuid 解析出昵称。
    """
    db = await _session_factory()
    try:
        user = (
            (await db.execute(select(User).where(User.username == username)))
            .scalars()
            .first()
        )
        if user is None:
            user = User(
                username=username,
                email=email or f"{username}@example.com",
                hashed_password=_SEED_PASSWORD_SENTINEL,
                account_level=account_level,
            )
            db.add(user)
            await db.flush()
        # 先取出主键再 commit：会话工厂若配了 expire_on_commit，commit 后再访问属性会触发
        # 惰性刷新，而 async 下同步属性访问会 MissingGreenlet。
        user_id = user.id

        # 用显式查询而非 user.profile：async 下访问未加载的 relationship 会 MissingGreenlet。
        profile = (
            (await db.execute(select(Profile).where(Profile.user_id == user_id)))
            .scalars()
            .first()
        )
        if profile is None:
            db.add(Profile(user_id=user_id, nickname=nickname))
        await db.commit()
        return user_id
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
