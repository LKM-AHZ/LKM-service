"""积分模块测试：并发 / 幂等 / 转账 / 流水一致性。

覆盖 reward/spend/transfer 的原子、幂等与余额界，排行榜排序、分页与路由鉴权。
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.core.err import BizError
from app.db.base import Base
from app.modules.points.errors import PointsErr
from app.modules.points.service import (
    get_balance,
    leaderboard,
    list_ledger,
    reward,
    spend,
    transfer,
)
from tests.conftest import auth_user_uid


async def _spend_in_session(
    factory: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    amount: int,
    ref_type: str,
    ref_id: str,
) -> None:
    """在独立会话中扣款并提交，用于构造真正的数据库级并发。"""
    async with factory() as s:
        await spend(s, uid, amount, "consume", ref_type, ref_id)
        await s.commit()


async def _user(
    auth_db: AsyncSession, username: str = "alice", nickname: str | None = None
) -> uuid.UUID:
    """在 auth realm 建一线用户返回其 uuid 主键（Nickname 落 auth Profile，供榜展示）。"""
    u = await auth_user_uid(
        auth_db, username=username, email=f"{username}@e.com", nickname=nickname
    )
    return u.id


class TestReward:
    async def test_reward_credits(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        entry = await reward(db, uid, 100, "test", "ref_t", "1")
        assert entry.delta == 100
        assert entry.balance_after == 100
        assert await get_balance(db, uid) == 100

    async def test_reward_idempotent(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 50, "test", "ref_t", "1")
        second = await reward(db, uid, 50, "test", "ref_t", "1")
        assert second.delta == 50
        assert await get_balance(db, uid) == 50  # 未重复加分

    async def test_reward_duplicate_delta_mismatch(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 50, "test", "ref_t", "1")
        with pytest.raises(BizError) as e:
            await reward(db, uid, 80, "test", "ref_t", "1")
        assert e.value.errcode == PointsErr.DUPLICATE_REWARD

    async def test_reward_negative_insufficient(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 30, "test", "a", "1")
        with pytest.raises(BizError) as e:
            await reward(db, uid, -50, "test", "b", "1")  # 30-50<0
        assert e.value.errcode == PointsErr.INSUFFICIENT_BALANCE

    async def test_reward_negative_allowed(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, -10, "penalty", "c", "1", allow_negative=True)
        assert await get_balance(db, uid) == -10


class TestSpend:
    async def test_spend_ok(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 100, "test", "x", "1")
        await spend(db, uid, 40, "consume", "x", "2")
        assert await get_balance(db, uid) == 60

    async def test_spend_insufficient(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 10, "test", "x", "1")
        with pytest.raises(BizError) as e:
            await spend(db, uid, 20, "consume", "x", "2")
        assert e.value.errcode == PointsErr.INSUFFICIENT_BALANCE


class TestTransfer:
    async def test_transfer_moves_balances(self, db: AsyncSession, auth_db: AsyncSession):
        a = await _user(auth_db, "a")
        b = await _user(auth_db, "b")
        await reward(db, a, 100, "test", "z", "1")
        out_e, in_e = await transfer(db, a, b, 40, "pay", "trx", "1")
        assert out_e.delta == -40
        assert in_e.delta == 40
        assert await get_balance(db, a) == 60
        assert await get_balance(db, b) == 40

    async def test_transfer_idempotent(self, db: AsyncSession, auth_db: AsyncSession):
        a = await _user(auth_db, "a")
        b = await _user(auth_db, "b")
        await reward(db, a, 100, "test", "z", "1")
        await transfer(db, a, b, 40, "pay", "trx", "1")
        # 重复同 ref → from 侧 transfer_out 行触发 (user, ref_type, ref_id) 唯一约束。
        # transfer 无 service 级幂等捕获，flush 直接抛 IntegrityError（非 BizError）。
        with pytest.raises(IntegrityError):
            await transfer(db, a, b, 40, "pay", "trx", "1")

    async def test_transfer_insufficient(self, db: AsyncSession, auth_db: AsyncSession):
        a = await _user(auth_db, "a")
        b = await _user(auth_db, "b")
        with pytest.raises(BizError) as e:
            await transfer(db, a, b, 40, "pay", "trx", "2")
        assert e.value.errcode == PointsErr.INSUFFICIENT_BALANCE


class TestConcurrency:
    async def test_concurrent_spend_no_overdraw(self, db: AsyncSession, auth_db: AsyncSession):
        """两个独立会话（各自事务）在真实 PG 上并发扣款 → 只有余额足够的那笔成功。

        目的：每个 spend 都跑在独立的 AsyncSession 上，让其在本进程的真实并发下
        竞争，从而覆盖 atomic 防透支守卫。

        说明：db 夹具是 schema-per-test 单长活会话，不宜拿来并发；此处在 biz 库建一个
        隔离 schema ``pts``，配 ``NullPool`` 引擎——每个并发会话独占一条连接、同指该 schema，
        走 PG 真实的 ``FOR UPDATE`` 行锁 + ``WHERE balance>=amount`` 守卫。两笔并发 spend：先
        者把 100 扣到 30 并提交；后者锁重整后余额不足 → 抛 BizError，恰一笔成功。
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import settings

        url = settings.database_url
        # schema 名含 pid：xdist 并行时各 worker 不撞名
        schema = f"pts_{os.getpid()}"
        # 先以默认连接干净建 schema
        boot = create_async_engine(url)
        async with boot.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await boot.dispose()
        # NullPool 每会话独立连接（server_settings 让每条都落该 schema）
        engine: AsyncEngine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        # 种子同 schema；points 的 user_id 是裸 uuid（无 identity/FK 依赖）→ 用字面 uuid 直接发分
        async with factory() as seed:
            uid = uuid.UUID("00000000-0000-7000-8000-000000000001")
            await reward(seed, uid, 100, "test", "cc", "0")
            await seed.commit()
        results = await asyncio.gather(
            _spend_in_session(factory, uid, 70, "cc", "1"),
            _spend_in_session(factory, uid, 70, "cc", "2"),
            return_exceptions=True,
        )
        succ = [x for x in results if not isinstance(x, Exception)]
        fail = [x for x in results if isinstance(x, Exception)]
        assert len(succ) == 1
        assert len(fail) == 1
        # 用一个新会话查余额 = 100 - 70 = 30（只有一个 70 成功）
        async with factory() as s:
            assert await get_balance(s, uid) == 30
        await engine.dispose()
        # 清场：drop 一次性 schema
        _clean = create_async_engine(url)
        try:
            async with _clean.begin() as conn:
                await conn.execute(text('DROP SCHEMA IF EXISTS "pts" CASCADE'))
        finally:
            await _clean.dispose()


class TestLeaderboard:
    async def test_leaderboard_orders(self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None):
        a = await _user(auth_db, "a", "甲")
        b = await _user(auth_db, "b", "乙")
        await reward(db, a, 50, "test", "l", "1")
        await reward(db, b, 100, "test", "l", "2")
        items, total = await leaderboard(db)
        assert total == 2
        assert items[0].user_id == b  # 乙 100 最高
        assert items[0].display_name == "乙"
        assert items[1].user_id == a

    async def test_leaderboard_tiebreak_by_display_name(self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None):
        """余额相等时按 display_name（昵称）字典序升序排。"""
        a = await _user(auth_db, "aaa", "Zebra")
        b = await _user(auth_db, "bbb", "Apple")
        await reward(db, a, 50, "test", "tb", "1")
        await reward(db, b, 50, "test", "tb", "2")
        items, _total = await leaderboard(db)
        assert (
            items[0].user_id == b and items[0].display_name == "Apple"
        )  # Apple < Zebra
        assert items[1].user_id == a and items[1].display_name == "Zebra"

    async def test_ledger_pagination(self, db: AsyncSession, auth_db: AsyncSession):
        uid = await _user(auth_db)
        await reward(db, uid, 1, "test", "pg", "1")
        await reward(db, uid, 2, "test", "pg", "2")
        udata = await list_ledger(db, uid, page=1, limit=1)
        assert udata.total == 2
        assert len(udata.items) == 1
        assert udata.pages == 2


class TestRouter:
    async def test_me_requires_auth(self, client, db):
        resp = await client.get("/api/v1/points/me")
        assert resp.status_code == 403

    async def test_leaderboard_public(self, client, db):
        resp = await client.get("/api/v1/points/leaderboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        # 已统一为 PageData（含 items/total/page/pages）并带 X-Total 头
        assert "items" in data and "total" in data
        assert resp.headers.get("X-Total") == str(data["total"])
