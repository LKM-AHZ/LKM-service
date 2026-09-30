"""跨进程指标中继（选项③）：清单完整性、生产者快照、消费者聚合、fail-open、生命周期。

不需要 PG、不需要真 Redis：生产/消费两侧都收一个可注入的 redis 形参，用内存替身驱动。
计数器的断言一律**取相对增量**（prometheus_client 的指标对象是进程内全局单例，跨用例累积）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from prometheus_client import REGISTRY

from core import metrics_relay
from core.config import settings


class _FakeRedis:
    """最小异步 Redis 替身：只实现中继用到的 set/get/scan_iter。

    ``fail_get`` 里的键读时抛错，用来钉「单键读取异常不拖垮其余实例」。
    """

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(store or {})
        self.fail_get: set[str] = set()
        self.last_ex: int | None = None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        self.last_ex = ex
        return True

    async def get(self, key: str) -> str | None:
        if key in self.fail_get:
            raise RuntimeError("boom")
        return self.store.get(key)

    async def scan_iter(self, match: str = "*"):
        prefix = match.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key


class _BrokenRedis(_FakeRedis):
    """scan_iter 整体抛错：钉「扫描失败置 up=0 且不抛」。"""

    async def scan_iter(self, match: str = "*"):
        raise RuntimeError("scan boom")
        yield  # pragma: no cover - 让它是生成器


@pytest.fixture(autouse=True)
def _isolate_relay_state() -> Any:
    """每测复位远端基线，并确保不留悬挂的发布/上报 task。"""
    metrics_relay._reset_remote_totals()
    yield
    metrics_relay._reset_remote_totals()


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """读指标现值；series 尚未出现时按 0 计。"""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _payload(*entries: tuple[str, list[str], float]) -> str:
    return json.dumps([list(e) for e in entries], separators=(",", ":"))


# ---- 清单完整性 ----


def test_relayed_specs_reference_real_metric_objects() -> None:
    """清单里的 metric 必须就是 core.metrics 的同名单例（改名会在装配期之外静默丢数）。"""
    from core import metrics as metrics_mod

    for spec in metrics_relay.RELAYED:
        assert getattr(metrics_mod, spec.name) is spec.metric, spec.name


def test_relayed_sample_names_match_declared_names() -> None:
    """``snapshot()`` 靠「样本名 == 声明名」取数——指标名/类型改动必须在此暴露。

    Counter 有 ``*_created`` 额外样本、prometheus_client 对 ``*_total`` 后缀还有一套归一化
    规则，靠肉眼看不出来，只能真 collect() 一次。带 label 的指标在没有 child 时 collect()
    为空，故用一个哨兵 label 值把 child 逼出来、断言完再 remove 掉（不留污染）。
    """
    for spec in metrics_relay.RELAYED:
        probe = ["__relay_probe__"] * len(spec.labelnames)
        if spec.labelnames:
            spec.metric.labels(*probe)
        try:
            names = {
                sample.name
                for metric in spec.metric.collect()
                for sample in metric.samples
            }
        finally:
            if spec.labelnames:
                spec.metric.remove(*probe)
        assert spec.name in names, (spec.name, sorted(names))
        assert any(n.endswith("_created") for n in names) == (spec.kind == "counter"), (
            spec.name
        )


def test_outbox_metrics_are_on_the_relay_list() -> None:
    """lkm-outbox.yml 的四条规则全部取数自这两条——掉出清单等于告警重新变哑。"""
    names = {spec.name for spec in metrics_relay.RELAYED}
    assert {"outbox_pending_count", "outbox_leader_total"} <= names


# ---- 生产者侧 ----


def test_snapshot_reports_current_values_with_labels() -> None:
    from core.metrics import outbox_leader_total, outbox_pending_count

    outbox_leader_total.labels("acquired").inc(2)
    outbox_pending_count.set(42)

    flat = {(name, tuple(lv)): value for name, lv, value in metrics_relay.snapshot()}

    assert flat[("outbox_leader_total", ("acquired",))] >= 2
    assert flat[("outbox_pending_count", ())] == 42


async def test_publish_snapshot_writes_key_with_ttl() -> None:
    from core.metrics import outbox_pending_count

    outbox_pending_count.set(9)
    redis = _FakeRedis()

    assert await metrics_relay.publish_snapshot(redis) is True

    raw = redis.store[metrics_relay.relay_key()]
    assert redis.last_ex == int(settings.metrics_relay_interval_s * 3)
    assert ["outbox_pending_count", [], 9] in json.loads(raw)


async def test_publish_snapshot_fail_open_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(*_a, **_k) -> None:
        return None

    monkeypatch.setattr("core.redis.get_redis", _none)
    assert await metrics_relay.publish_snapshot() is False


async def test_publish_snapshot_fail_open_when_set_raises() -> None:
    class _Raising(_FakeRedis):
        async def set(self, key: str, value: str, ex: int | None = None) -> bool:
            raise RuntimeError("set boom")

    # 不抛即通过（fail-open 契约）
    assert await metrics_relay.publish_snapshot(_Raising()) is False


# ---- 消费者侧：计数器增量 ----


async def test_collect_once_sums_instances_as_delta() -> None:

    redis = _FakeRedis(
        {
            metrics_relay.relay_key("a"): _payload(
                ("outbox_leader_total", ["acquired"], 3)
            ),
            metrics_relay.relay_key("b"): _payload(
                ("outbox_leader_total", ["acquired"], 4)
            ),
        }
    )
    before = _sample("outbox_leader_total", {"event": "acquired"})

    await metrics_relay.collect_once(redis)

    # 两实例求和 = 7，一次性作为增量落进本地计数器
    assert _sample(
        "outbox_leader_total", {"event": "acquired"}
    ) - before == pytest.approx(7)
    assert _sample("metrics_relay_instances") == 2
    assert _sample("metrics_relay_up") == 1


async def test_collect_once_is_not_cumulative() -> None:
    """同一份快照连读两次不得重复计数（远端是**累计值**，只有增量才落账）。"""

    redis = _FakeRedis(
        {
            metrics_relay.relay_key("a"): _payload(
                ("outbox_leader_total", ["contended"], 5)
            )
        }
    )
    before = _sample("outbox_leader_total", {"event": "contended"})

    await metrics_relay.collect_once(redis)
    once = _sample("outbox_leader_total", {"event": "contended"})
    await metrics_relay.collect_once(redis)
    twice = _sample("outbox_leader_total", {"event": "contended"})

    assert once - before == pytest.approx(5)
    assert twice == once


async def test_collect_once_rebaselines_on_remote_reset() -> None:
    """远端计数回退（worker 重启令其进程内计数归零）只重置基线，绝不让本地计数回退。"""

    key = metrics_relay.relay_key("a")
    redis = _FakeRedis({key: _payload(("outbox_leader_total", ["renew_failed"], 10))})
    before = _sample("outbox_leader_total", {"event": "renew_failed"})

    await metrics_relay.collect_once(redis)
    after_first = _sample("outbox_leader_total", {"event": "renew_failed"})
    assert after_first - before == pytest.approx(10)

    # 模拟该 worker 重启：它的本地计数从 0 重新长到 4
    redis.store[key] = _payload(("outbox_leader_total", ["renew_failed"], 4))
    await metrics_relay.collect_once(redis)
    assert _sample("outbox_leader_total", {"event": "renew_failed"}) == after_first

    # 之后的新增量照常落账（4 → 6 记 +2，而不是 6-10 的负数）
    redis.store[key] = _payload(("outbox_leader_total", ["renew_failed"], 6))
    await metrics_relay.collect_once(redis)
    assert _sample(
        "outbox_leader_total", {"event": "renew_failed"}
    ) - after_first == pytest.approx(2)


# ---- 消费者侧：gauge 取 max ----


async def test_collect_once_takes_max_for_gauges() -> None:
    from core.metrics import outbox_pending_count

    redis = _FakeRedis(
        {
            metrics_relay.relay_key("a"): _payload(("outbox_pending_count", [], 10)),
            # follower 副本的快照里积压是 0（它从不轮询）——不得把 leader 的读数拉低
            metrics_relay.relay_key("b"): _payload(("outbox_pending_count", [], 0)),
        }
    )

    await metrics_relay.collect_once(redis)

    assert outbox_pending_count._value.get() == 10


async def test_collect_once_keeps_last_gauge_when_no_source() -> None:
    """来源消失时保留上次值并把 up 置 0——写 0 会假装「积压已清零」。"""
    from core.metrics import outbox_pending_count

    redis = _FakeRedis(
        {metrics_relay.relay_key("a"): _payload(("outbox_pending_count", [], 7))}
    )
    await metrics_relay.collect_once(redis)
    assert _sample("metrics_relay_instances") == 1

    redis.store.clear()
    await metrics_relay.collect_once(redis)

    assert outbox_pending_count._value.get() == 7
    assert _sample("metrics_relay_instances") == 0
    assert _sample("metrics_relay_up") == 0


# ---- fail-open ----


async def test_collect_once_fail_open_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(*_a, **_k) -> None:
        return None

    monkeypatch.setattr("core.redis.get_redis", _none)
    await metrics_relay.collect_once()  # 不抛
    assert _sample("metrics_relay_up") == 0


async def test_collect_once_fail_open_on_scan_error() -> None:
    await metrics_relay.collect_once(_BrokenRedis())  # 不抛
    assert _sample("metrics_relay_up") == 0


async def test_collect_once_skips_corrupt_and_unreadable_keys() -> None:
    """坏载荷 / 单键读失败只能影响它自己，其余实例照常入账。"""
    good = metrics_relay.relay_key("good")
    corrupt = metrics_relay.relay_key("corrupt")
    redis = _FakeRedis(
        {
            good: _payload(("outbox_leader_total", ["stale_reclaimed"], 2)),
            # 非 JSON / 非数组：载荷损坏 → 不计入存活实例
            corrupt: "{not json",
            metrics_relay.relay_key("wrongshape"): json.dumps({"a": 1}),
            metrics_relay.relay_key("unreadable"): _payload(
                ("outbox_leader_total", ["acquired"], 1)
            ),
        }
    )
    redis.fail_get.add(corrupt)
    redis.fail_get.add(metrics_relay.relay_key("unreadable"))
    before = _sample("outbox_leader_total", {"event": "stale_reclaimed"})

    await metrics_relay.collect_once(redis)

    # 两个坏键被跳过、good 键仍入账 → instances = 1
    assert _sample(
        "outbox_leader_total", {"event": "stale_reclaimed"}
    ) - before == pytest.approx(2)
    assert _sample("metrics_relay_instances") == 1
    assert _sample("metrics_relay_up") == 1


async def test_collect_once_ignores_unrecognized_entries_but_counts_instance() -> None:
    """是本进程不认识的条目（版本偏斜）就只丢那一条：实例仍算活着、其余条目照常入账。"""
    redis = _FakeRedis(
        {
            metrics_relay.relay_key("skewed"): json.dumps(
                [
                    # 名不在清单（旧/新进程多了个指标）
                    ["nope_total", [], 1],
                    # label 个数与清单不符
                    ["outbox_leader_total", ["acquired", "extra"], 1],
                    # 值非数字
                    ["outbox_leader_total", ["acquired"], "x"],
                    # 形状不对
                    [1, 2, 3],
                    # 这一条是好的
                    ["outbox_leader_total", ["acquired"], 3],
                ]
            )
        }
    )
    before = _sample("outbox_leader_total", {"event": "acquired"})

    await metrics_relay.collect_once(redis)

    assert _sample("outbox_leader_total", {"event": "acquired"}) - before == (
        pytest.approx(3)
    )
    # 载荷是合法 JSON 数组 → 生产者活着，仍计入 instances（只是条目被忽略）
    assert _sample("metrics_relay_instances") == 1


# ---- 生命周期 ----


async def test_reporter_lifecycle_is_idempotent() -> None:
    metrics_relay.start_reporter()
    task = metrics_relay._task
    assert task is not None
    metrics_relay.start_reporter()
    assert metrics_relay._task is task, "重复 start 不得再起一个 task"

    await metrics_relay.stop_reporter()
    assert metrics_relay._task is None
    await metrics_relay.stop_reporter()  # 可重复调用


async def test_publisher_lifecycle_is_idempotent() -> None:
    metrics_relay.start_publisher()
    task = metrics_relay._publisher
    assert task is not None
    metrics_relay.start_publisher()  # run_default_worker 的 4 个 _consume 会同款重复调用
    assert metrics_relay._publisher is task

    await metrics_relay.stop_publisher()
    assert metrics_relay._publisher is None
    await metrics_relay.stop_publisher()


async def test_lifecycle_is_a_noop_when_metrics_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "metrics_enabled", False)
    metrics_relay.start_reporter()
    metrics_relay.start_publisher()
    assert metrics_relay._task is None
    assert metrics_relay._publisher is None


async def test_run_publisher_writes_then_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布循环真实走一遍：写一次快照 + 下界 sleep（interval 配 0 不得变成忙等）。"""
    calls: list[float] = []

    async def _publish(redis: Any | None = None) -> bool:
        calls.append(1.0)
        return True

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(metrics_relay, "publish_snapshot", _publish)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await metrics_relay.run_publisher(interval_s=0)

    assert calls == [1.0, 1.0], "interval<=0 必须被下界夹到 1s"
