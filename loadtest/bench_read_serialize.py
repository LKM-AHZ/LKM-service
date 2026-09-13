"""读热序列化微基准（roadmap §6.5.2，M5）——非 pytest，直接运行：

    uv run python loadtest/bench_read_serialize.py

两段：
1. **timeline 响应序列化**：对 20/50/100 条合成 ``FeedResponse``，比较既有路径
   （Pydantic ``ApiResp.model_dump(mode="json")`` + stdlib ``json.dumps``，即 ``resp_json`` 实际做的事）
   与 msgspec 路径（Pydantic 校验后 ``feed.wire.to_wire`` + ``core.wire.msgspec_ok`` 直出 bytes）。
   决策门槛：加速 ≥1.5x 且单请求省 ≥30µs 才保持 ``LKM_READ_MSGPEC_ENABLED`` 默认开启。
2. **L1/L2 user:snap 读**：fakeredis 预填 L2，比较 ``L1 开/关`` 下 ``user_cache.read_snap`` 的
   ops/s，为上一阶段 L1 缓存补「启用前后非恶化」基线。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
import sys
import time
from collections.abc import Callable
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.common import ApiResp
from app.core.wire import msgspec_ok
from app.modules.feed.schemas import FeedItem, FeedResponse
from app.modules.feed.wire import to_wire

_N = 0.5  # 每个测量点的最短采样时长（秒）
_SPEEDUP_MIN = 1.5
_US_SAVED_MIN = 30.0


def make_feed(n: int) -> FeedResponse:
    """合成 n 条跨源条目（含 None/中文/tz/负 float），贴近真实多字段列表。"""
    base = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    types = ["article", "discussion", "column", "qa", "project", "blog"]
    items = [
        FeedItem(
            item_type=types[i % len(types)],
            id=i + 1,
            author_id=None if i % 7 == 0 else i + 100,
            author_name="张三" if i % 3 == 0 else f"user{i}",
            title=f"标题 {i} — 多字段列表序列化基准",
            content_preview="内容预览" * 4,
            created_at=base + datetime.timedelta(minutes=i),
            sort_score=(-1.0 if i % 5 == 0 else 1.0) * (i + 0.5),
            board_id=None if i % 4 == 0 else (i % 9) + 1,
            url=f"/content/{i + 1}",
        )
        for i in range(n)
    ]
    return FeedResponse(items=items, next_cursor="Y3Vyc29y|42" if n else None)


def _baseline_bytes(resp: FeedResponse) -> bytes:
    """既有路径：resp_json 的 model_dump(mode="json") + JSONResponse 的 json.dumps 参数。"""
    content = ApiResp(code=0, msg="OK", data=resp).model_dump(mode="json")
    return json.dumps(
        content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _msgspec_bytes(resp: FeedResponse) -> bytes:
    """msgspec 路径：校验后镜像 + 预编码 Response 的 body。"""
    return msgspec_ok(to_wire(resp)).body


def measure(fn: Callable[[], Any], min_seconds: float = _N) -> tuple[float, int]:
    """warmup 后循环至 min_seconds，返回 (ops/s, 单次 µs)。"""
    for _ in range(200):
        fn()
    iters = 0
    start = time.perf_counter()
    deadline = start + min_seconds
    while time.perf_counter() < deadline:
        fn()
        iters += 1
    elapsed = time.perf_counter() - start
    per_s = iters / elapsed
    return per_s, 1_000_000 / per_s


def bench_serialize() -> bool:
    print("== timeline 响应序列化（20/50/100 条）==")
    print(
        f"{'n':>4} | {'baseline ops/s':>14} | {'msgspec ops/s':>13} | {'speedup':>7} | {'µs saved':>8} | json_eq"
    )
    all_pass = True
    for n in (20, 50, 100):
        resp = make_feed(n)
        old_b, old_us = measure(lambda r=resp: _baseline_bytes(r))
        new_b, new_us = measure(lambda r=resp: _msgspec_bytes(r))
        eq = json.loads(_baseline_bytes(resp)) == json.loads(_msgspec_bytes(resp))
        speedup = old_us / new_us if new_us else 0.0
        saved = old_us - new_us
        ok = eq and speedup >= _SPEEDUP_MIN and saved >= _US_SAVED_MIN
        all_pass = all_pass and ok
        print(
            f"{n:>4} | {old_b:>14,.0f} | {new_b:>13,.0f} | {speedup:>6.2f}x | "
            f"{saved:>7.1f} | {eq}"
        )
    print(
        f"门槛: speedup>={_SPEEDUP_MIN}x 且省>={_US_SAVED_MIN}µs/req → "
        f"{'保持默认开启' if all_pass else '建议关闭默认'}\n"
    )
    return all_pass


async def _enable_fake_redis() -> Any:
    import fakeredis.aioredis

    import app.core.redis as redis_mod
    from app.core.config import settings

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings.redis_url = "redis://localhost:6379/0"

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    redis_mod.Redis.from_url = classmethod(_from_url)  # type: ignore[method-assign]
    redis_mod._client = None
    redis_mod._client_pool = None
    return fake


async def bench_l1() -> None:
    import app.core.user_cache as uc
    from app.core.config import settings

    await _enable_fake_redis()
    snap = {
        "user_id": 7,
        "username": "bob",
        "display_name": "Bob",
        "avatar": None,
        "role": None,
        "account_level": "local",
        "banned": False,
        "nickname": None,
    }

    async def one_read() -> None:
        await uc.read_snap(7)

    async def run_with(l1: bool) -> tuple[float, float]:
        settings.user_snap_l1_enabled = l1
        await uc.write_if_newer(7, snap, source_version=1, expected_epoch=0)
        # warmup + 采样
        for _ in range(200):
            await one_read()
        iters = 0
        start = time.perf_counter()
        deadline = start + _N
        while time.perf_counter() < deadline:
            await one_read()
            iters += 1
        elapsed = time.perf_counter() - start
        per_s = iters / elapsed
        return per_s, 1_000_000 / per_s

    on_s, on_us = await run_with(True)
    off_s, off_us = await run_with(False)
    print("== user:snap L1 vs L2 read（fakeredis）==")
    print(f"{'L1':>5} | {'ops/s':>12} | {'µs/read':>8}")
    print(f"{'on':>5} | {on_s:>12,.0f} | {on_us:>8.2f}")
    print(f"{'off':>5} | {off_s:>12,.0f} | {off_us:>8.2f}")
    print(f"L1 加速: {off_us / on_us if on_us else 0:.2f}x（非劣化判据：on <= off）\n")


if __name__ == "__main__":
    bench_serialize()
    asyncio.run(bench_l1())
