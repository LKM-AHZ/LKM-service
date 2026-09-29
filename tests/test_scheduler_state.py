"""调度器运行态的跨进程暴露（蓝图 §5.5 第 6 条）。

调度器在独立 worker-scheduler 进程、不暴露 /metrics，故运行态经 **Redis 心跳**交给 API
进程的 reporter 上报（与 pulsar lag 同一范式）。本文件验三段硬性质：
① 心跳写入（载荷 + TTL）；② reporter 读到新鲜心跳 → gauge 正确；③ **读不到心跳 → up=0**
（进程没了/卡死/Redis 断都表现为「不可认为在跑」，这正是「生命周期异常可观测」）。
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
from prometheus_client import REGISTRY

from app.core import scheduler_state


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _gauge(name: str) -> float:
    value = REGISTRY.get_sample_value(name)
    return value if value is not None else -1.0


class TestHeartbeatWrite:
    async def test_writes_payload_and_ttl(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        scheduler_state.note_started(7)
        scheduler_state.note_job_started()

        assert await scheduler_state.write_heartbeat(fake_redis, interval_s=10.0) is True

        raw = await fake_redis.get(scheduler_state.HEARTBEAT_KEY)
        assert json.loads(raw) == {"state": 1, "jobs": 7, "pending": 1}
        # TTL = 3×周期：允许漏两拍而不误判 down
        ttl = await fake_redis.ttl(scheduler_state.HEARTBEAT_KEY)
        assert 0 < ttl <= 30

        scheduler_state.note_job_finished()

    async def test_returns_false_without_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _none() -> None:
            return None

        monkeypatch.setattr(scheduler_state, "get_redis", _none)
        assert await scheduler_state.write_heartbeat() is False


class TestReporter:
    async def test_fresh_heartbeat_sets_gauges(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        scheduler_state.note_started(5)
        await scheduler_state.write_heartbeat(fake_redis, interval_s=10.0)

        await scheduler_state.collect_once(fake_redis)

        assert _gauge("scheduler_up") == 1.0
        assert _gauge("scheduler_state") == 1.0
        assert _gauge("scheduler_jobs") == 5.0
        assert _gauge("scheduler_pending_jobs") == 0.0

    async def test_paused_state_is_reported(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        scheduler_state.note_started(5)
        scheduler_state.note_stopped()
        await scheduler_state.write_heartbeat(fake_redis, interval_s=10.0)

        await scheduler_state.collect_once(fake_redis)

        assert _gauge("scheduler_up") == 1.0  # 进程还在，只是暂停
        assert _gauge("scheduler_state") == 0.0

    async def test_missing_heartbeat_marks_down(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """键不存在（进程亡/卡死/TTL 过期）→ up=0、state=0。"""
        await scheduler_state.collect_once(fake_redis)

        assert _gauge("scheduler_up") == 0.0
        assert _gauge("scheduler_state") == 0.0

    async def test_corrupt_payload_marks_down(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        await fake_redis.set(scheduler_state.HEARTBEAT_KEY, "{not json")

        await scheduler_state.collect_once(fake_redis)

        assert _gauge("scheduler_up") == 0.0

    async def test_unreadable_redis_marks_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis 不可用 → 不知道调度器状态，按不可认为在跑处置（宁可吵不可沉默）。"""

        async def _none() -> None:
            return None

        monkeypatch.setattr(scheduler_state, "get_redis", _none)
        await scheduler_state.collect_once()

        assert _gauge("scheduler_up") == 0.0
