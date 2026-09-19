"""A7 auth 用户变更事件 → user:snap 失效 的验收（M3.A.2 后半 —— 失效侧）。

被测对象：AUTH 各快照相关写点按源发 ``event.user.{updated,banned,session_revoke}`` outbox
事件；消费侧 ``auth.tasks.invalidate_user_snap`` 把事件变成 ``core.user_cache`` 的
``del + epoch bump`` 失效（A7 调用口），保证下次 ``get_user_snapshot`` 从 DB 拉到新值、
陈旧缓存不复活。

为何分三层（settings.pulsar_url 单测默认空 → ``enqueue_outbox`` fail-open 直接不入队，
无真 relay/E2E；见 brief）：
1) 真 outbox 行断言：monkeypatch pulsar_url 非空，走**真实写点**（update_profile /
   upgrade_to_normal / _reset_password / notify_user_banned_committed）后 commit，查
   ``OutboxMessage`` 的 routing_key + payload.args 断言落对源事件。
2) consumer 契约：直接调 ``auth.tasks.invalidate_user_snap``（worker 分派会跑的 handler），
   断言缓存被 del/epoch bump、幂等可重跑。
3) HARD 新鲜度（profile 变更）：真 Profile 编改(update_profile) → 经上面同样机制失效 →
   ``get_user_snapshot`` 回到 DB 拉到**新 nickname**（绝非改前缓存的旧值）。此步走真实缝，
   确定性、无需消息总线 worker（失效 handler 在进程内直接驱动，等价 worker 分派）。
"""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import app.core.redis as redis_mod
import app.core.user_cache as uc
from app.core.config import settings
from app.db.base import Base
from app.db.outbox import OutboxMessage
from auth import events as auth_events
from auth.models import Profile, User
from auth.schemas import ProfileUpdate
from auth.security import hashpwd
from auth.service import update_profile
from auth.service_auth import upgrade_to_normal
from auth.service_recovery import _reset_password as reset_password_svc
from auth.tasks import invalidate_user_snap
from tests.conftest import DB


# S5 拆库后 auth(User/Profile) 在 auth_metadata、biz(outbox/user_dim)在 Base —— 本文件真写点需
# “融合单一 PG schema”（Base+auth_metadata 双 create_all，无表名冲突）承载，auth/biz 单会话可见
# （等同拆库收尾终态）。跨多会话落此 schema 用 asyncpg connect_args.server_settings.search_path。
@pytest.fixture
async def _fused_realm():
    from sqlalchemy import text

    from app.db.model_registry import ensure_all_models
    from auth.db.base import auth_metadata

    ensure_all_models()
    url = settings.database_url
    schema = f"uci_{os.getpid()}"
    boot = create_async_engine(url)
    async with boot.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await boot.dispose()
    engine = create_async_engine(
        url,
        poolclass=StaticPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(auth_metadata.create_all)
    maker = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield engine, maker
    finally:
        await engine.dispose()


@pytest.fixture
async def db(_fused_realm) -> AsyncSession:
    """覆盖 conftest.db：跑在融合 auth+biz schema 的长活会话（auth 写点/outbox 单会话可见）。"""
    _engine, maker = _fused_realm
    async with maker() as s:
        yield s


# B0.2：consumer handler invalidate_user_snap 现还会顺带离线刷新 user_dim(自开会话 seam)。
# 指向同一融合库的独立会话工厂，作 fail-open 写目标（不与真实默认 DB 纠缠）。
@pytest.fixture(autouse=True)
async def _dim_sync_throwaway(monkeypatch, _fused_realm) -> None:
    _engine, maker = _fused_realm

    async def _factory() -> tuple[AsyncSession, AsyncSession]:
        # 融合 schema：源(auth)与目标(user_dim)同库，双会话指向同一 maker。
        s = maker()
        return s, s

    monkeypatch.setattr(
        "auth.user_dim_sync._session_factory", _factory
    )



@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    """复位 redis 模块单例 + 复位消息总线配置，杜绝跨测试残留（repo 范式的 autouse reset）。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


def _enable_fake_redis(monkeypatch: Any) -> None:
    """get_redis → fakeredis（decode_responses=True），与 test_user_snap_cache_cas 一致。"""
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))


def _enable_bus(monkeypatch: Any) -> None:
    """打开 outbox 门控：单测才真正把事件行落库（生产 relay/worker 才消费，此处断言行即可）。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://localhost:6650")


async def _enabled() -> Any:
    c = await redis_mod.get_redis()
    assert c is not None
    return c


async def _mk_user(
    db: AsyncSession, username: str, *, nickname: str = "旧名", account_level: str = "normal"
) -> uuid.UUID:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret12345!"),
        account_level=account_level,
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, nickname=nickname, role="member"))
    await db.flush()
    return user.id


async def _rows(db: AsyncSession) -> list[Any]:
    """查全部 pending outbox 事件行（断言源发落点用）。"""
    res = await db.execute(
        select(OutboxMessage).where(OutboxMessage.status == "pending").order_by(OutboxMessage.id)
    )
    return list(res.scalars().all())


def _payload_user_id(payload_json: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(payload_json["args"][0]))


# ---- Layer 1：真实写点按源发对应 outbox 事件 ----
class TestMutationSitesEmitOutboxEvents:
    async def test_profile_edit_emits_user_updated(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_bus(monkeypatch)
        uid = await _mk_user(db, "prof_edit")
        await update_profile(db, uid, ProfileUpdate(nickname="新名"))
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1
        r = rows[0]
        assert r.routing_key == "event.user.updated"
        assert _payload_user_id(r.payload_json) == uid

    async def test_upgrade_to_normal_emits_user_updated(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_bus(monkeypatch)
        uid = await _mk_user(db, "up_level", account_level="local")
        from sqlalchemy.orm import selectinload

        user = (
            (
                await db.execute(
                    select(User).where(User.id == uid).options(selectinload(User.profile))
                )
            )
            .scalars()
            .one()
        )
        await upgrade_to_normal(db, user)
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1
        assert rows[0].routing_key == "event.user.updated"

    async def test_password_reset_emits_user_session_revoke(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_bus(monkeypatch)
        uid = await _mk_user(db, "pwd_reset")
        from sqlalchemy.orm import selectinload

        user = (
            (
                await db.execute(
                    select(User).where(User.id == uid).options(selectinload(User.profile))
                )
            )
            .scalars()
            .one()
        )
        await reset_password_svc(db, user, "newsecret99!!")
        await db.commit()

        rows = await _rows(db)
        ks = {r.routing_key for r in rows}
        assert "event.user.session_revoke" in ks
        # 事件的 payload 都指向同一 user
        for r in rows:
            assert _payload_user_id(r.payload_json) == uid

    async def test_account_lock_committed_emits_user_banned(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自动锁定路径独立提交 banned 事件（自建会话指向测试库）。"""
        _enable_bus(monkeypatch)
        uid = await _mk_user(db, "lock_user")
        # 沿用 files.notify 测法：把新的 own-session 指向测试 db，事件行落同一库便于断言。
        monkeypatch.setattr(
            "auth.events.new_session", _session_for(db)
        )
        await auth_events.notify_user_banned_committed(uid)
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1
        assert rows[0].routing_key == "event.user.banned"
        assert _payload_user_id(rows[0].payload_json) == uid


def _session_for(db: AsyncSession):
    async def _new() -> AsyncSession:
        return db

    return _new


# ---- Layer 2：consumer handler 契约（幂等失效原语）----
class TestConsumerHandlerInvalidates:
    async def test_invalidate_user_snap_dels_and_bumps_epoch(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        e = await uc.current_epoch(uid)
        assert await uc.write_if_newer(
            uid, {"display_name": "old"}, source_version=10, expected_epoch=e
        )
        assert await uc.read_snap(uid) == {"display_name": "old"}

        # worker 分派对 payload fn=invalidate_user_snap args=[uid] 会展开调用同名 handler
        await invalidate_user_snap(uid)

        assert await uc.read_snap(uid) is None          # 缓存已空（del 生效）
        assert await uc.current_epoch(uid) == e + 1      # epoch 反陈旧 bump
        # 幂等：再跑一次不抛、不破坏（epoch 继续单增，缓存仍空）
        await invalidate_user_snap(uid)
        assert await uc.read_snap(uid) is None
        assert await uc.current_epoch(uid) == e + 2


# ---- HARD：profile 变更后经单缝读必拉新 nickname（绝不 stale）----
class TestProfileEditFreshnessThroughSeam:
    async def test_next_snapshot_returns_new_nickname(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from auth.snapshot import get_user_snapshot

        _enable_fake_redis(monkeypatch)
        uid = await _mk_user(db, "freshness", nickname="旧名")
        await db.commit()
        snap_before = await get_user_snapshot(db, user_id=uid)
        assert snap_before is not None and snap_before.display_name == "旧名"
        assert await uc.read_snap(uid) is not None  # 已缓存"旧"

        # 真人 profile 编改（只改 Profile，User.updated_at 不会被 onupdate 抬动——A6 关键窗口）
        await update_profile(db, uid, ProfileUpdate(nickname="焕然一新"))
        await db.commit()

        # consumer 失效（与 worker 分派一致的 handler）
        await invalidate_user_snap(uid)
        assert await uc.read_snap(uid) is None

        # 重新经真实快照缝读：必须拉到 DB 新值，绝非失效前缓存的"旧名"
        snap_after = await get_user_snapshot(db, user_id=uid)
        assert snap_after is not None
        assert snap_after.display_name == "焕然一新"
        assert snap_after != snap_before


# ---- M3.A残项: 成功登录解锁(is_locked True→False)须失效 user:snap，patch banned 陈旧 ----
class TestLoginUnlockInvalidatesSnap:
    async def _mk_locked_user(
        self, db: AsyncSession, username: str, password: str
    ) -> uuid.UUID:
        user = User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=await hashpwd(password),
            account_level="normal",
        )
        db.add(user)
        await db.flush()
        uid = user.id  # commit 前置 ID，避免 commit 后 expire 触发 greenlet 懒读
        user.is_locked = True  # 模拟先前被自动锁定(banned)，locked_until=None(解锁后可成功登)
        await db.commit()
        return uid

    async def test_successful_unlock_emits_event_and_next_read_banned_false(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from auth.schemas import UserLoginPassword
        from auth.service_auth import login_password
        from auth.snapshot import get_user_snapshot

        _enable_fake_redis(monkeypatch)
        _enable_bus(monkeypatch)
        password = "secret12345!"
        uid = await self._mk_locked_user(db, "unlockme", password)

        # 复现存量 stale:user:snap.banned=True 已入缓存（post-user.banned 后未失效）
        u_row = (await db.execute(select(User).where(User.id == uid))).scalars().one()
        e = await uc.current_epoch(uid)
        sv = uc.version_of_updated_at(u_row.updated_at) if u_row.updated_at else 1
        assert await uc.write_if_newer(
            uid, {"banned": True, "display_name": u_row.username}, sv, e
        )
        assert (await uc.read_snap(uid))["banned"] is True  # 缓存此刻仍是 stale banned=True

        # 成功登录（密码对）→ is_locked True→False 翻转
        await login_password(
            db, UserLoginPassword(account="unlockme", password=password)
        )
        await db.commit()  # 持久化 outbox 事件行

        # ① 解锁真实翻转 → 事件入队(event.user.updated, 指向该 user)，普通登(falsg→False)不于此断言去重
        rows = await _rows(db)
        ks = [r.routing_key for r in rows]
        assert "event.user.updated" in ks
        matching = [r for r in rows if _payload_user_id(r.payload_json) == uid]
        assert matching, "successful-unlock 应就 uid 发 user.updated outbox 事件"

        # ② online 读：consumer 消费失效(与 worker 同 handler) → 缓存空，直读 DB banned=False 不再 stale True
        await invalidate_user_snap(uid)
        assert await uc.read_snap(uid) is None
        post = await get_user_snapshot(db, user_id=uid)
        assert post is not None and post.banned is False
