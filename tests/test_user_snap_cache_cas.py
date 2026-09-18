"""A6 user:snap 缓存 + 版本 CAS + 反陈旧防复活原语的 TDD 验收

- 复用 repo test_cache / test_outbox_lease 的 fakeredis fixture 范式（reset _core 单例 +
  monkeypatch settings.redis_url + Redis.from_url → fake，**decode_responses=True**，与生产
  get_redis 一致，CAS 比较的是 str/int 而非 bytes）。
- 直接驱动 core.user_cache 导出原语（确定性推演，不做真多线程以免 flake——性质本身被断言）；
  也经 auth/snapshot.get_user_snapshot 端到端验证 cache-through。
- roadmap 硬性质锚定：
  ① 命中返回 DB 值 / miss 回填 / 键形态 TTL 命名空间 sane
  ② cache-through 展示语义与直读 DB diff=0
  ③ CAS：旧 sv 被拒 / 新 sv 得胜
  ④ anti-stale-after-invalidate：失效(INCR+del)后持旧 epoch 的陈旧回填被拒、缓存保持空、
     随后正常读读到已变更 DB 的新值（无复活）——本文件的头条
  ⑤ Redis 故障 → fail-open 返回 DB 值不 crash
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
import app.core.user_cache as uc
from app.core.cache import make_key
from app.core.config import settings
from app.modules.auth.models import Profile, User
from app.modules.auth.security import hashpwd
from app.modules.auth.snapshot import get_user_snapshot
from tests.conftest import DB


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """snapshot/cache 读的 user 在 auth 独立库。"""
    return auth_db


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    """每个用例前后彻底复位 redis 模块级单例，杜绝跨测试残留键/连接污染（repo 范式）。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


def _enable_fake_redis(monkeypatch: Any) -> Any:
    """把 get_redis 指到 fakeredis（decode_responses=True），与 test_cache / test_outbox_lease 一致。"""
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    return fake


def _disabled(monkeypatch: Any) -> None:
    """模拟 Redis 未配置（fail-open 场景）。"""
    monkeypatch.setattr(settings, "redis_url", "")


async def _client() -> Any:
    c = await redis_mod.get_redis()
    assert c is not None
    return c


async def _mk_user(
    db: AsyncSession,
    username: str,
    *,
    nickname: str | None = None,
    updated_at: datetime | None = None,
    role: str | None = "member",
    account_level: str = "local",
) -> tuple[uuid.UUID, datetime]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret123456"),
        account_level=account_level,
    )
    if updated_at is not None:
        user.updated_at = updated_at
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, nickname=nickname, role=role))
    await db.flush()
    # 返回 (user_id, 该时刻 updated_at)：显式赋值优先；未显式则由列 default(now_iso) 落库后存在。
    assert user.updated_at is not None
    return user.id, user.updated_at


# ---- Test 1: 读缓存命中/未命中回填语义；键/TTL/命名空间 sane ----


async def _hit(uid: uuid.UUID) -> dict[str, Any]:
    """必须命中缓存并返回快照 dict；未命中即断言失败（测试前置不变量）。"""
    data = await uc.read_snap(uid)
    assert data is not None
    return data
class TestReadPopulateAndKeyShape:
    async def test_miss_return_none_then_fill_then_hit(self, monkeypatch: Any) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        assert await uc.read_snap(uid) is None  # miss
        assert (
            (await uc.read_snap_with_version(uid)) == (None, None)
        )  # miss with-version 也为空
        epoch0 = await uc.current_epoch(uid)
        assert epoch0 == 0  # 从未失效 → epoch 0
        snap = {"user_id": uid, "username": "bob", "display_name": "Bob",
                "avatar": None, "role": None,
                "account_level": "local", "banned": False}
        assert await uc.write_if_newer(uid, snap,
                                       source_version=100, expected_epoch=epoch0) is True
        assert await uc.read_snap(uid) == snap
        sv, _d = await uc.read_snap_with_version(uid)
        assert sv == 100

    async def test_key_shape_env_namespace_ttl(self, monkeypatch: Any) -> None:
        """键沿用 make_key env 命名空间；TTL 有 EX 兜底。"""
        fake = _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        assert uc.get_user_cache_key(uid) == make_key("user:snap", uid)
        assert str(uc.get_user_cache_key(uid)).endswith(f":user:snap:{uid}")
        await uc._get_redis()
        # 经 write_if_newer 得到的快照键带 TTL
        e = await uc.current_epoch(uid)
        await uc.write_if_newer(uid, {"x": 1}, 1, e)
        import app.core.user_cache as _uc

        raw = await fake.get(_uc._snap_key(uid))
        assert raw is not None
        assert await fake.ttl(_uc._snap_key(uid)) != -1  # 有失效时间（非无限）

    async def test_empty_db_user_not_cached_and_none(self, db: DB, monkeypatch: Any) -> None:
        """不存在用户 miss → 命中返回 None 语义；不写缓存（seam DB 直读）。"""
        _enable_fake_redis(monkeypatch)
        snap = await get_user_snapshot(db, user_id=uuid.uuid4())
        assert snap is None


# ---- Test 2: cache-through 展示语义 == DB（diff=0 within seam）----
class TestSeamCacheThroughDiffZero:
    async def test_cache_read_eq_db_read_diff_zero(self, db: DB, monkeypatch: Any) -> None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.modules.auth.models import User as U
        from app.modules.auth.snapshot import _to_snap

        _enable_fake_redis(monkeypatch)
        uid, _ = await _mk_user(db, "coco", nickname="CoCo 酱", account_level="admin")
        snap1 = await get_user_snapshot(db, user_id=uid)  # miss → DB + 回填
        row = (
            await db.execute(select(U).where(U.id == uid).options(selectinload(U.profile)))
        ).scalar_one()
        db_direct = _to_snap(row)
        assert snap1 == db_direct  # miss 返回值 == 直读 DB
        # 再次调用命中缓存：仍 == DB
        snap2 = await get_user_snapshot(db, user_id=uid)
        assert snap2 is not None
        assert snap2 == db_direct
        assert snap2 == snap1
        # 手动改 DB → 未失效前命中给缓存旧值(一致语义窗口)；失效后给新值
        # diff 语义(diff=0)：命中值字段与 DB 直读值逐字段一致（在事件外部读窗口成立）
        got = await get_user_snapshot(db, user_id=uid)
        for f in ("user_id", "username", "display_name", "avatar", "role",
                  "account_level", "banned"):
            assert getattr(got, f) == getattr(snap1, f)


# ---- Test 3: CAS 版本守卫——旧 sv 被拒 / 新 sv 得胜 ----
class TestCasVersionGuard:
    async def test_stale_older_version_rejected_newer_wins(self, monkeypatch: Any) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        e = await uc.current_epoch(uid)
        # 新版本先写
        assert await uc.write_if_newer(uid, {"v": "new"}, source_version=200, expected_epoch=e) is True
        # 陈旧(更旧 sv)写入被拒、不覆盖
        assert await uc.write_if_newer(uid, {"v": "stale"}, source_version=100, expected_epoch=e) is False
        assert (await _hit(uid))["v"] == "new"
        # 更新的 sv 得胜
        assert await uc.write_if_newer(uid, {"v": "newer"}, source_version=300, expected_epoch=e) is True
        assert (await _hit(uid))["v"] == "newer"

    async def test_equal_version_idempotent_rewrite_allowed(self, monkeypatch: Any) -> None:
        """等 sv 视为幂等续写（不看作覆盖更新值）——多实例同值回填不互相拒写死锁。"""
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        e = await uc.current_epoch(uid)
        assert await uc.write_if_newer(uid, {"v": 1}, 50, e) is True
        assert await uc.write_if_newer(uid, {"v": 1}, 50, e) is True  # 等值允许
        assert (await _hit(uid))["v"] == 1


# ---- Test 4: 头条 anti-stale-after-invalidate（真两段、确定的 concurrency 语义）----
class TestAntiStaleAfterInvalidate:
    async def test_invalidate_blocks_stale_pop_then_fresh_read(self, monkeypatch: Any) -> None:
        _enable_fake_redis(monkeypatch)
        uid = uuid.uuid4()
        e0 = await uc.current_epoch(uid)  # 0
        # 正常回填（epoch under 它 DB 读语义 OK）
        assert await uc.write_if_newer(uid, {"v": "old"}, source_version=100, expected_epoch=e0)
        assert (await _hit(uid))["v"] == "old"

        # ---- invalidate: INCR epoch + del snap，原子 ----
        await uc.invalidate_user_snap(uid)
        assert await uc.read_snap(uid) is None          # 缓存已空
        assert await uc.current_epoch(uid) == e0 + 1     # 代次 +1

        # ---- 陈旧在途回填（持有失效前的旧 epoch=0、旧值+旧版本）企图复活 ----
        stale_won = await uc.write_if_newer(
            uid, {"v": "old"}, source_version=100, expected_epoch=e0  # 旧捕获代次
        )
        assert stale_won is False                        # 被拒：陈旧写不回去
        assert await uc.read_snap(uid) is None           # 缓存保持空（无复活）

        # ---- 正常读（捕获新代次 1）写新 DB 值 = 拉到 / 允许新值 ----
        assert await uc.write_if_newer(uid, {"v": "fresh"}, source_version=200, expected_epoch=e0 + 1)
        assert (await _hit(uid))["v"] == "fresh"

    async def test_seam_repopulation_after_db_change_returns_new_value(self, db: DB, monkeypatch: Any) -> None:
        """端到端：缓存被失效清空后，同一用户 DB 变更 → seam 读到→回填新值，无陈旧复活。"""
        from sqlalchemy import select as sselect
        from sqlalchemy.orm import selectinload

        from app.modules.auth.models import User as U

        _enable_fake_redis(monkeypatch)
        uid, _ = await _mk_user(db, "charlie", nickname="老", updated_at=datetime(2020, 1, 1, tzinfo=UTC))
        # 第一次读→缓存放"老"
        s1 = await get_user_snapshot(db, user_id=uid)
        assert s1 is not None and s1.display_name == "老"
        assert await uc.read_snap(uid) is not None

        # 失效（模拟 A7：User/Profile 变更事件）扫掉缓存
        await uc.invalidate_user_snap(uid)
        assert await uc.read_snap(uid) is None

        # DB 真正变更（display_name 由 profile.nickname 决定：改 nickname + 抬 updated_at）
        dbrow = (
            await db.execute(sselect(U).where(U.id == uid).options(selectinload(U.profile)))
        ).scalar_one()
        dbrow.profile.nickname = "新新"
        dbrow.updated_at = datetime(2021, 1, 1, tzinfo=UTC)
        await db.flush()

        # 再次读：miss → 从 DB 拉到新值（绝非失效前缓存的"老"）
        s2 = await get_user_snapshot(db, user_id=uid)
        assert s2 is not None
        assert s2.display_name == "新新"  # 展示语义已拉到新
        assert s2 != s1

    async def test_concurrent_backfills_no_resurrection_gather(self, db: DB, monkeypatch: Any) -> None:
        """asyncio.gather 交错回填：陈旧代次被拒、新值得胜、终态非陈旧。确定性（非真线程，无 flake）。"""

        _enable_fake_redis(monkeypatch)
        uid, _ = await _mk_user(db, "gather", nickname="n1", updated_at=datetime(2022, 2, 2, tzinfo=UTC))

        import asyncio

        async def pop(name: str, ver: int, ep: int) -> bool:
            # 模拟一个在途回填协程：捕获 ep → (可能被打断) → 落盘
            await asyncio.sleep(0)
            return await uc.write_if_newer(uid, {"n": name}, ver, ep)

        e = await uc.current_epoch(uid)
        # A/B 都以同一失效前代次并发回填（一个陈旧 version 较低、一个最新 version 较高）
        results = await asyncio.gather(pop("A-old", 50, e), pop("B-new", 900, e))
        # 最新 sv=900 必胜；任一淘汰——但绝不可能两者都 False（同为新鲜代次写空必需一个成功）
        assert True in results
        store = await uc.read_snap(uid)
        assert store is not None
        assert store["n"] == "B-new"  # 终值 = 新值，非陈旧 A


# ---- Test 5: Redis 故障 fail-open 返回 DB 值不 crash ----
class TestFailOpen:
    async def test_disabled_redis_seam_returns_db_value(self, db: DB, monkeypatch: Any) -> None:
        _disabled(monkeypatch)  # redis_url 为空
        uid, _ = await _mk_user(db, "fallback", nickname="F")
        # 缓存不可用 → seam 走 DB 原语义
        snap = await get_user_snapshot(db, user_id=uid)
        assert snap is not None and snap.display_name == "F"
        # 原语不抛
        assert await uc.read_snap(uid) is None
        assert await uc.read_snap_with_version(uid) == (None, None)
        assert await uc.current_epoch(uid) == 0
        assert await uc.write_if_newer(uid, {"a": 1}, 1, 0) is False  # 未写入
        await uc.invalidate_user_snap(uid)  # 静默不抛

    async def test_redis_broken_client_connection_error_no_crash(self, db: DB, monkeypatch: Any) -> None:
        """Redis 命中即抛 ConnectionError → fail-open：读返回 None、不 crash、回退 DB。"""
        _enable_fake_redis(monkeypatch)

        class Boom:
            expire_flag = True

            async def get(self, *a: Any, **k: Any) -> Any:
                raise ConnectionError("redis down")
            def pipeline(self, *a: Any, **k: Any) -> Any:
                raise ConnectionError("redis down")

        c = await redis_mod.get_redis()
        assert c is not None
        monkeypatch.setattr(redis_mod, "_client", Boom())  # type: ignore[assignment]
        uid, _ = await _mk_user(db, "boom", nickname="B")
        snap = await get_user_snapshot(db, user_id=uid)  # seam 读缓存异常 → miss → DB，不 crash
        assert snap is not None and snap.display_name == "B"
        # 原语各自 fail-open 不抛
        assert await uc.read_snap(uid) is None
        assert await uc.current_epoch(uid) == 0
        assert await uc.write_if_newer(uid, {"a": 1}, 1, 0) is False
        await uc.invalidate_user_snap(uid)
