"""§5.2 ``audit.*`` 事件家族：发射点 + 消费侧。

背景（改动前）：审计只写 auth 库 ``audit_logs`` 表、再由批任务灌 ClickHouse，**总线上零
audit 事件**——属「事后可查」，无法及时发现正在进行的暴力破解/越权探测。本文件守住新增的
实时链路：

1. **发射点**：失败密码登录发 ``audit.login_fail``；``grant_*`` 真实升权发
   ``audit.permission_change``（无改动不发）。
2. **回滚场景**（本链路最容易写错的地方）：失败登录必然让外层请求抛 ``BizError`` 并回滚，
   任何挂在该事务上的 outbox 行都会一起丢——事件必须**自建会话独立提交**。故这里让
   own-session 指向**同库的另一个会话**（不是复用测试会话），这样「外层回滚、事件仍在」
   才是被真正证明的，而不是被同一个会话顺带提交掉。
3. **消费契约**：``auth.tasks.record_audit_event`` 递增 ``audit_events_total{action}``；
   未知 action 被忽略（label 无界 = 指标基数爆炸）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.err import BizError
from app.core.messaging import (
    RKEY_AUDIT_LOGIN_FAIL,
    RKEY_AUDIT_PERMISSION_CHANGE,
    SUBSCRIPTIONS,
    TOPIC_AUDIT_LOGIN_FAIL,
    TOPIC_AUDIT_PERMISSION_CHANGE,
)
from app.core.task_registry import handlers_for, import_task_modules
from app.db.outbox import OutboxMessage
from auth.models import Profile, User
from auth.schemas import UserLoginPassword
from auth.security import hashpwd
from auth.service_auth import login_password
from auth.service_authz import grant_incubation
from auth.tasks import record_audit_event


@pytest.fixture
async def db(fused_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """覆盖 conftest.db：跑在**融合库**（auth users/profile + biz outbox 单库可见）。

    并把 ``auth.events`` 的自建会话指向同库的**另一个** session（而非测试会话）——独立提交
    语义靠这个区分，复用测试会话会让「回滚后事件仍在」变成假绿。
    """
    maker = async_sessionmaker(fused_db_session.bind, expire_on_commit=False)

    async def _own_session() -> AsyncSession:
        return maker()

    monkeypatch.setattr("auth.events.new_session", _own_session)
    yield fused_db_session


@pytest.fixture(autouse=True)
def _enable_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开 outbox 门控：单测才真正把事件行落库（生产由 relay/worker 消费，此处只断言行）。"""
    monkeypatch.setattr(settings, "pulsar_url", "pulsar://localhost:6650")


async def _mk_user(db: AsyncSession, username: str, *, password: str = "secret12345!") -> uuid.UUID:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd(password),
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, nickname=username, role="member"))
    await db.flush()
    return user.id


async def _rows(db: AsyncSession, routing_key: str) -> list[Any]:
    res = await db.execute(
        select(OutboxMessage)
        .where(OutboxMessage.routing_key == routing_key)
        .order_by(OutboxMessage.id)
    )
    return list(res.scalars().all())


async def test_failed_login_emits_audit_event_that_survives_rollback(
    db: AsyncSession,
) -> None:
    """失败登录：事件必须落库，且**外层事务回滚后仍在**（自建会话独立提交）。"""
    uid = await _mk_user(db, "audit_fail")
    await db.commit()

    with pytest.raises(BizError):
        await login_password(
            db, UserLoginPassword(account="audit_fail", password="wrong-password"), "1.2.3.4"
        )
    # 请求以回滚收场（get_session 在抛错时的真实行为）
    await db.rollback()

    rows = await _rows(db, RKEY_AUDIT_LOGIN_FAIL)
    assert len(rows) == 1, "失败登录的审计事件被外层回滚吞掉了"
    payload = rows[0].payload_json
    assert payload["fn"] == "record_audit_event"
    assert payload["args"][0] == str(uid)
    assert "password_mismatch" in payload["args"][2]
    assert "1.2.3.4" in payload["args"][2]  # 来源 IP 供撞库定位


async def test_successful_login_emits_no_audit_event(db: AsyncSession) -> None:
    """成功登录不发 audit.login_fail（否则告警会被正常流量淹没）。"""
    await _mk_user(db, "audit_ok")
    await db.commit()

    await login_password(
        db, UserLoginPassword(account="audit_ok", password="secret12345!"), "1.2.3.4"
    )
    await db.commit()

    assert await _rows(db, RKEY_AUDIT_LOGIN_FAIL) == []


async def test_permission_change_emitted_only_when_something_changed(
    db: AsyncSession,
) -> None:
    """真实升权才发 audit.permission_change；无改动不发（同事务入队）。"""
    uid = await _mk_user(db, "audit_grant")
    await db.commit()

    assert await grant_incubation(db, uid) == 1
    await db.commit()
    rows = await _rows(db, RKEY_AUDIT_PERMISSION_CHANGE)
    assert len(rows) == 1
    assert rows[0].payload_json["args"][0] == str(uid)
    assert rows[0].payload_json["args"][2] == "grant_incubation"

    # 已经是 admin + incubated_member → 无改动，不该再发一条
    assert await grant_incubation(db, uid) == 0
    await db.commit()
    assert len(await _rows(db, RKEY_AUDIT_PERMISSION_CHANGE)) == 1


def _audit_value(action: str) -> float:
    val = REGISTRY.get_sample_value("audit_events_total", {"action": action})
    return val if val is not None else 0.0


async def test_consumer_records_metric_and_ignores_unknown_action() -> None:
    """消费侧：已知 action 递增指标；未知 action 直接忽略（避免无界 label）。"""
    before = _audit_value(RKEY_AUDIT_LOGIN_FAIL)

    await record_audit_event(str(uuid.uuid4()), RKEY_AUDIT_LOGIN_FAIL, "password_mismatch")
    await record_audit_event(None, RKEY_AUDIT_PERMISSION_CHANGE, "grant_exam_unlock")
    await record_audit_event(None, "audit.not_a_thing", "whatever")

    assert _audit_value(RKEY_AUDIT_LOGIN_FAIL) == before + 1
    assert _audit_value(RKEY_AUDIT_PERMISSION_CHANGE) >= 1
    assert REGISTRY.get_sample_value("audit_events_total", {"action": "audit.not_a_thing"}) is None


async def test_wiring_topics_and_handler_registered() -> None:
    """接线契约：audit.* 家族两个 topic 各有订阅，且 payload.fn 在注册表里有 handler。"""
    assert SUBSCRIPTIONS["audit"].topic == TOPIC_AUDIT_LOGIN_FAIL
    assert SUBSCRIPTIONS["audit"].routing_keys == (RKEY_AUDIT_LOGIN_FAIL,)
    assert SUBSCRIPTIONS["audit-permission"].topic == TOPIC_AUDIT_PERMISSION_CHANGE
    assert SUBSCRIPTIONS["audit-permission"].routing_keys == (
        RKEY_AUDIT_PERMISSION_CHANGE,
    )

    import_task_modules()
    assert handlers_for("audit")["record_audit_event"] is record_audit_event
    assert handlers_for("audit-permission")["record_audit_event"] is record_audit_event
