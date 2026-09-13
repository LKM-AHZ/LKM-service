"""M5 7.2.1 hypothesis 属性测试：outbox 退避 / 入队幂等 / relay 不重复不丢失。

与 ``test_outbox.py`` 的确定性用例互补：这里用随机化的 attempt、event_id 集合与
publish 成功/失败序列，断言**不变量**而非单点。绝不触真 Pulsar/Redis——只
monkeypatch ``messaging.publish`` 为内存实现，只驱动纯函数 ``relay_poll``
（leader 租约只出现在 ``run_outbox_loop``，不在此覆盖）。

``@given`` 为同步函数，内部经 ``tests.prop_pg.PropPG`` 的持久事件循环执行异步场景。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from sqlalchemy import delete, func, select, update

from app.core import messaging, outbox_relay
from app.core.config import settings
from app.db.event_failure import EventFailure
from app.db.outbox import (
    _BACKOFF_CAP_S,
    MAX_TRIES,
    OUTBOX_PUBLISHED,
    OutboxMessage,
    _backoff_seconds,
    enqueue_outbox,
)
from tests.prop_pg import PropPG

_RK = "event.apply_point"
_PAYLOAD = {"fn": "apply_point_event", "args": [7, "post", "item:9"]}


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件默认「已配置消息总线」，放行 enqueue gate（镜像 test_outbox.py）。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://props:6650")


# ───────────────────────── 1) 退避：单调非降 + 封顶（纯函数） ─────────────────────────


@given(st.integers(min_value=0, max_value=2000))
def test_backoff_monotonic_and_bounded(attempt: int) -> None:
    v = _backoff_seconds(attempt)
    assert 1 <= v <= _BACKOFF_CAP_S
    # 指数退避单调非降（封顶后恒等）
    assert _backoff_seconds(attempt + 1) >= v


# ───────────────────────── 2) enqueue 显式 event_id 幂等 ─────────────────────────


async def _enqueue_scenario(pg: PropPG, ids: list[str]) -> None:
    db = pg.session()
    try:
        await db.execute(delete(OutboxMessage))
        await db.commit()
    finally:
        await db.close()

    seen: set[str] = set()
    for eid in ids:
        db = pg.session()
        try:
            added = await enqueue_outbox(db, _RK, _PAYLOAD, event_id=eid)
            await db.commit()
        finally:
            await db.close()
        if eid in seen:
            assert added is False, f"重复 event_id {eid!r} 应被幂等跳过"
        else:
            assert added is True
            seen.add(eid)

    db = pg.session()
    try:
        n = await db.scalar(select(func.count()).select_from(OutboxMessage))
    finally:
        await db.close()
    # 行数 = 去重后的 event_id 数：重复入队不产生第二行
    assert n == len(set(ids))


@hsettings(max_examples=25, deadline=None)
@given(
    st.lists(
        st.text(alphabet="abcxyz0123456789-", min_size=1, max_size=16),
        min_size=0,
        max_size=8,
    )
)
def test_enqueue_idempotent_by_event_id(ids: list[str]) -> None:
    with PropPG("p_outbox_enqueue") as pg:
        pg.run(_enqueue_scenario(pg, ids))


# ───────────────────────── 3) relay：不重复投递、不退丢、达上限折叠 ─────────────────────────


async def _relay_scenario(pg: PropPG, specs: list[tuple[str, list[bool]]]) -> None:
    db = pg.session()
    try:
        await db.execute(delete(EventFailure))
        await db.execute(delete(OutboxMessage))
        for eid, _ in specs:
            await enqueue_outbox(db, _RK, _PAYLOAD, event_id=eid)
        await db.commit()
    finally:
        await db.close()

    patterns = dict(specs)
    idx: dict[str, int] = dict.fromkeys(patterns, 0)
    success: dict[str, int] = dict.fromkeys(patterns, 0)

    async def _pub(_rk: str, payload: dict) -> bool:
        eid = payload["event_id"]
        i = idx[eid]
        idx[eid] = i + 1
        pat = patterns[eid]
        ok = pat[i] if i < len(pat) else False
        if ok:
            success[eid] += 1
        return ok

    # 不经 monkeypatch fixture：@given 与 pytest fixture 混用受限，直接在模块级
    # messaging.publish 上替换并在 finally 还原。
    original = messaging.publish
    messaging.publish = _pub  # type: ignore[assignment]
    try:
        # 每轮前把所有 pending 的 next_retry_at 拨回过去，让退避中的事件下一轮立即可领。
        for _ in range(MAX_TRIES + 2):
            db = pg.session()
            try:
                await db.execute(
                    update(OutboxMessage)
                    .where(OutboxMessage.status == "pending")
                    .values(next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await db.commit()
            finally:
                await db.close()
            await outbox_relay.relay_poll(session_factory=pg.session_factory)
    finally:
        messaging.publish = original  # type: ignore[assignment]

    db = pg.session()
    try:
        rows = (await db.execute(select(OutboxMessage))).scalars().all()
        folds = (await db.execute(select(EventFailure))).scalars().all()
    finally:
        await db.close()

    row_by_id = {r.event_id: r for r in rows}
    fold_by_id = {f.event_id: f for f in folds}

    for eid, pat in patterns.items():
        # 前 MAX_TRIES 次尝试内出现成功 → 应恰好 published 一次
        expected_pub = any(pat[i] for i in range(min(len(pat), MAX_TRIES)))
        assert success[eid] <= 1, f"{eid!r} 被重复投递 {success[eid]} 次"
        if expected_pub:
            assert success[eid] == 1
            row = row_by_id.get(eid)
            assert row is not None and row.status == OUTBOX_PUBLISHED
            assert row.published_at is not None
            assert eid not in fold_by_id
        else:
            assert success[eid] == 0
            assert eid not in row_by_id, f"{eid!r} 达上限后未摘出 outbox"
            assert fold_by_id[eid].attempt_count == MAX_TRIES
            assert "relay exhausted" in fold_by_id[eid].reason

    # 无丢无重：published 行 + 折叠行 = 入队事件总数
    assert len(rows) + len(folds) == len(patterns)


@hsettings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.text(alphabet="abcxyz0123456789-", min_size=1, max_size=16),
            st.lists(st.booleans(), max_size=MAX_TRIES + 1),
        ),
        min_size=1,
        max_size=4,
        unique_by=lambda t: t[0],
    )
)
def test_relay_never_duplicates_or_loses(
    specs: list[tuple[str, list[bool]]],
) -> None:
    with PropPG("p_outbox_relay") as pg:
        pg.run(_relay_scenario(pg, specs))
