"""auth 事件投递在**真拆库拓扑**下必须可用（2026-09-26 真机验收缺陷的回归测试）。

缺陷原貌：auth 侧把 outbox 事件行 join 到**调用方传入的会话**（"同事务入队"），而拆库后

* ``outbox_events`` 属**业务库**表；
* auth 侧请求持有的是 **auth 库**会话（``get_auth_session``，users/profiles 唯属 auth 库）。

于是 commit/flush 打到 ``lkm_auth`` → ``relation "outbox_events" does not exist`` →
**整个请求 500**。真机上的触发点是「已存在账号走注册入口」（``_login_or_error`` →
``upgrade_to_normal`` → ``notify_user_updated``），它同时让 ``grant_*`` 与
``audit.permission_change`` 在拆库下全线不可用。

本文件用**两个真库**复现该拓扑（与融合库测试相反，这里刻意让调用方会话**看不到**
``outbox_events``）：

- ``auth_db``：auth 独立库 schema（AuthBase），**没有** outbox_events —— 即调用方会话；
- ``db``：业务库 schema（Base），有 outbox_events —— 即事件应当落到的地方。

两条断言缺一不可：① 写点**不抛**（原缺陷就是抛 500）；② 事件确实落进**业务库**。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

import app.core.redis as redis_mod
from app.core.config import settings
from app.db.outbox import OutboxMessage
from auth.models import Profile, User
from auth.security import hashpwd
from auth.service_auth import upgrade_to_normal
from auth.service_authz import grant_incubation


@pytest.fixture(autouse=True)
async def _bus_on(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """打开 outbox 门控（否则 ``enqueue_outbox`` fail-open 直返，测不到投递）。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://localhost:6650")
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


@pytest.fixture(autouse=True)
def _events_use_business_db(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``auth.events`` 的自建会话指向**业务库**测试会话所在的 engine。

    生产里 ``app.db.session.new_session()`` 本就解析到业务库；测试里必须显式指向本测的
    业务库 schema，否则会连到默认库、断言看不到行。
    """
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def _new() -> AsyncSession:
        return maker()

    monkeypatch.setattr("auth.events.new_session", _new)


async def _mk_auth_user(
    auth_db: AsyncSession, username: str, *, account_level: str = "normal"
) -> uuid.UUID:
    """在 **auth 独立库**建用户（业务库没有 users 表，正是拆库的本意）。"""
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret12345!"),
        account_level=account_level,
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, nickname=username, role="member"))
    await auth_db.flush()
    return user.id


async def _business_outbox(db: AsyncSession, routing_key: str) -> list[OutboxMessage]:
    res = await db.execute(
        select(OutboxMessage)
        .where(OutboxMessage.routing_key == routing_key)
        .order_by(OutboxMessage.id)
    )
    return list(res.scalars().all())


async def test_auth_db_has_no_outbox_table(auth_db: AsyncSession) -> None:
    """前提守卫：auth 库确实没有 outbox_events。

    若哪天 auth 库也建了 outbox_events，本文件其余用例会「假绿」——故先把前提钉住。
    """
    exists = await auth_db.scalar(
        text("select to_regclass('public.outbox_events') is not null")
    )
    assert exists is False


async def test_upgrade_to_normal_does_not_raise_and_emits_to_business_db(
    auth_db: AsyncSession, db: AsyncSession
) -> None:
    """已存在账号升级（原缺陷的触发路径）：不抛，且事件落业务库。"""
    uid = await _mk_auth_user(auth_db, "split_up", account_level="local")
    user = (
        (
            await auth_db.execute(
                select(User).where(User.id == uid).options(selectinload(User.profile))
            )
        )
        .scalars()
        .one()
    )

    await upgrade_to_normal(auth_db, user)  # 原缺陷在此抛 ProgrammingError
    await auth_db.commit()

    rows = await _business_outbox(db, "event.user.updated")
    assert len(rows) == 1
    assert rows[0].payload_json["args"][0] == str(uid)


async def test_permission_change_audit_reaches_business_db(
    auth_db: AsyncSession, db: AsyncSession
) -> None:
    """升权审计（§5.2）：拆库下同样必须投得出——原缺陷让它整条链不可用。"""
    uid = await _mk_auth_user(auth_db, "split_grant")

    assert await grant_incubation(auth_db, uid) == 1
    await auth_db.commit()

    assert len(await _business_outbox(db, "event.user.updated")) == 1
    audits = await _business_outbox(db, "audit.permission_change")
    assert len(audits) == 1
    assert audits[0].payload_json["args"][0] == str(uid)
    assert audits[0].payload_json["args"][2] == "grant_incubation"


async def test_event_survives_caller_rollback(
    auth_db: AsyncSession, db: AsyncSession
) -> None:
    """调用方回滚**不影响**事件已投出（独立提交的直接体现）。"""
    uid = await _mk_auth_user(auth_db, "split_rollback", account_level="local")
    await auth_db.commit()  # 用户先落库，下面要单独回滚「升级」这一步

    async def _level() -> str:
        return str(
            await auth_db.scalar(select(User.account_level).where(User.id == uid))
        )

    user = (
        (
            await auth_db.execute(
                select(User).where(User.id == uid).options(selectinload(User.profile))
            )
        )
        .scalars()
        .one()
    )

    await upgrade_to_normal(auth_db, user)
    await auth_db.rollback()  # 模拟请求以回滚收场

    # 事件仍在业务库；auth 库里的升级则被回滚
    assert len(await _business_outbox(db, "event.user.updated")) == 1
    assert await _level() == "local"
