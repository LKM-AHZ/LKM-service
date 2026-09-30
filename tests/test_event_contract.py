"""事件契约（``app/core/event_contract.py``）的单测。

背景：broker 侧的 ``schemaValidationEnforced=true`` **不校验 payload**（2026-09-30 真机实测，
见该模块 docstring），所以「事件契约」只能落在应用层。本文件守住三件事：

1. **完备性**：契约登记表 ↔ handler 注册表 ↔ routing_key 表三者互相覆盖，谁漏谁红；
2. **有牙**：违约 payload 在发布期被拒（并计数）、消费期被丢弃、relay 侧判为永久失败；
3. **装配期校验有牙**：fn 未登记 / 形参元数不符 / 订阅承载不了 → ``register_task`` 直接抛。
"""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.core import event_contract, messaging, task_registry, worker
from tests.fakes import InMemoryTransport


@pytest.fixture(autouse=True)
def _reset_transport():
    messaging.set_transport(None)
    yield
    messaging.set_transport(None)


def _violations(fn: str, side: str) -> float:
    value = REGISTRY.get_sample_value(
        "event_contract_violations_total",
        {"fn": event_contract.violation_label(fn), "side": side},
    )
    return value if value is not None else 0.0


# ───────────────────────── 1) 完备性：三张表互相覆盖 ─────────────────────────


def test_contracts_cover_registered_handlers() -> None:
    """每个已注册 handler 的 fn 都必须有契约，且每个契约都要有人消费（防单侧漏登记）。"""
    task_registry.import_task_modules()
    registered = {fn for table in task_registry._TASK_HANDLERS.values() for fn in table}
    declared = set(event_contract.EVENT_CONTRACTS)

    assert registered - declared == set(), (
        f"这些 handler 的 fn 没有事件契约：{sorted(registered - declared)}"
    )
    assert declared - registered == set(), (
        f"这些契约没有任何 handler 消费：{sorted(declared - registered)}"
    )


def test_every_routing_key_is_carried_by_some_contract() -> None:
    """每个逻辑 routing_key 都必须是某个 fn 的合法承载者（否则它有 topic 却没人声明契约）。"""
    covered = {
        rk for c in event_contract.EVENT_CONTRACTS.values() for rk in c.routing_keys
    }
    assert set(messaging.ROUTING_KEY_TOPICS) - covered == set()


def test_cron_declarations_conform_to_contracts() -> None:
    """cron 声明必须与契约一致：fn 已登记、无实参、routing_key 在允许集合内。"""
    task_registry.import_task_modules()
    jobs = task_registry.cron_jobs()
    assert jobs, "cron 声明为空，本测试失去意义"
    for job in jobs:
        contract = event_contract.EVENT_CONTRACTS.get(job["fn"])
        assert contract is not None, f"cron {job['id']} 的 fn={job['fn']} 无契约"
        assert contract.args == (), (
            f"cron {job['id']} 的 fn 带实参契约，scheduler 却只发 fn"
        )
        assert job["routing_key"] in contract.routing_keys, job["id"]


# ───────────────────────── 2) 登记表自检有牙 ─────────────────────────


def test_registry_selfcheck_rejects_optional_before_required() -> None:
    bogus = {
        "x": event_contract.EventContract(
            "x",
            (messaging.RKEY_NOTIFY,),
            (
                event_contract.Arg("a", "str", optional=True),
                event_contract.Arg("b", "str"),
            ),
        )
    }
    with pytest.raises(ValueError, match="必填实参"):
        event_contract._validate_registry(bogus)


def test_registry_selfcheck_rejects_unknown_kind() -> None:
    bogus = {
        "x": event_contract.EventContract(
            "x", (messaging.RKEY_NOTIFY,), (event_contract.Arg("a", "uuid"),)
        )
    }
    with pytest.raises(ValueError, match="类型未知"):
        event_contract._validate_registry(bogus)


def test_registry_selfcheck_rejects_unknown_routing_key() -> None:
    bogus = {
        "x": event_contract.EventContract(
            "x", ("event.does_not_exist",), (event_contract.Arg("a", "str"),)
        )
    }
    with pytest.raises(ValueError, match="未知 routing_key"):
        event_contract._validate_registry(bogus)


# ───────────────────────── 3) 装配期校验（register_task / register_cron_job） ─────────────────────────


async def _dummy(*args: Any) -> None: ...


def test_register_task_rejects_unregistered_fn() -> None:
    with pytest.raises(ValueError, match="未登记事件契约"):
        task_registry.register_task("notify", "no_such_fn", _dummy)


def test_register_task_rejects_arity_mismatch() -> None:
    """handler 接不住契约的实参个数 → 装配期就炸（原先要等消息到了才 TypeError）。"""

    async def _too_many(a, b, c, d): ...  # notify_upload 契约只有 1 个实参

    with pytest.raises(ValueError, match="必填形参"):
        task_registry.register_task("notify", "notify_upload", _too_many)


def test_register_task_rejects_subscription_that_cannot_carry_fn() -> None:
    """订阅的 routing_keys 承载不了该 fn → handler 永不触发，必须装配期报错。"""

    async def _h(upload_id): ...

    with pytest.raises(ValueError, match="承载不了"):
        task_registry.register_task("send", "notify_upload", _h)


def test_register_task_accepts_conforming_handler() -> None:
    """正例：形参元数与契约相容、订阅能承载 → 正常登记（用完复原，不污染全局注册表）。"""
    task_registry.import_task_modules()
    original = task_registry._TASK_HANDLERS["notify"]["notify_upload"]

    async def _h(upload_id): ...

    try:
        task_registry.register_task("notify", "notify_upload", _h)
        assert task_registry.handlers_for("notify")["notify_upload"] is _h
    finally:
        task_registry._TASK_HANDLERS["notify"]["notify_upload"] = original


def test_register_cron_job_rejects_fn_with_args() -> None:
    with pytest.raises(ValueError, match="带实参契约"):
        task_registry.register_cron_job(
            job_id="bogus",
            cron="* * * * *",
            routing_key="event.apply_point",
            fn="apply_point_event",
        )


def test_register_cron_job_rejects_disallowed_routing_key() -> None:
    with pytest.raises(ValueError, match="契约只允许"):
        task_registry.register_cron_job(
            job_id="bogus",
            cron="* * * * *",
            routing_key="cron.ops_daily",
            fn="reconcile_user_dim",
        )


# ───────────────────────── 4) 校验器：合规与违约的边界 ─────────────────────────


@pytest.mark.parametrize(
    ("routing_key", "payload"),
    [
        ("event.notify_upload", {"fn": "notify_upload", "args": ["u1"]}),
        (
            "event.notify_upload",
            {"fn": "notify_upload", "args": ["u1"], "event_id": "e"},
        ),
        ("event.apply_point", {"fn": "apply_point_event", "args": ["u", "post", "p1"]}),
        (
            "audit.login_fail",
            {"fn": "record_audit_event", "args": [None, "audit.login_fail"]},
        ),
        (
            "audit.login_fail",
            {
                "fn": "record_audit_event",
                "args": ["u", "audit.login_fail", "bad password"],
            },
        ),
        ("cron.reconcile", {"fn": "reconcile_user_dim"}),
    ],
)
def test_validator_accepts_conforming(routing_key: str, payload: dict) -> None:
    assert event_contract.payload_violations(payload, routing_key) == []


@pytest.mark.parametrize(
    ("case", "routing_key", "payload", "match"),
    [
        (
            "未知 fn",
            "event.notify_upload",
            {"fn": "nope", "args": []},
            "未登记的事件 fn",
        ),
        ("fn 缺失", "event.notify_upload", {"args": []}, "fn 缺失"),
        ("fn 非字符串", "event.notify_upload", {"fn": 1, "args": []}, "fn 缺失"),
        (
            "多余 envelope 键",
            "event.notify_upload",
            {"fn": "notify_upload", "args": ["u"], "extra": 1},
            "未知 envelope 键",
        ),
        ("实参不足", "event.notify_upload", {"fn": "notify_upload"}, "实参数不足"),
        (
            "实参过多",
            "event.notify_upload",
            {"fn": "notify_upload", "args": ["a", "b"]},
            "实参数过多",
        ),
        (
            "实参类型不符",
            "event.notify_upload",
            {"fn": "notify_upload", "args": [1]},
            "类型不符",
        ),
        (
            "args 非数组",
            "event.notify_upload",
            {"fn": "notify_upload", "args": "u"},
            "args 非数组",
        ),
        (
            "routing_key 不承载该 fn",
            "event.apply_point",
            {"fn": "notify_upload", "args": ["u"]},
            "不得经",
        ),
        (
            "event_id 非字符串",
            "event.notify_upload",
            {"fn": "notify_upload", "args": ["u"], "event_id": 7},
            "event_id",
        ),
        ("payload 非对象", None, ["not"], "payload 非对象"),
    ],
)
def test_validator_rejects_violations(
    case: str, routing_key: str | None, payload: Any, match: str
) -> None:
    problems = event_contract.payload_violations(payload, routing_key)
    assert any(match in p for p in problems), f"{case}: {problems}"


def test_bool_is_not_accepted_where_int_expected() -> None:
    """``bool`` 是 ``int`` 的子类，但线上 True/1 语义不同，必须拒绝（避免静默跑错分支）。"""
    contract = event_contract.EventContract(
        "x", (messaging.RKEY_NOTIFY,), (event_contract.Arg("n", "int"),)
    )
    assert event_contract._value_matches(True, contract.args[0]) is False
    assert event_contract._value_matches(1, contract.args[0]) is True


def test_nullable_only_where_declared() -> None:
    """``user_id`` 在审计事件里可空、在 user.* 失效事件里不可空——同一位置上二者相反。"""
    assert (
        event_contract.payload_violations(
            {"fn": "record_audit_event", "args": [None, "audit.login_fail"]},
            "audit.login_fail",
        )
        == []
    )
    problems = event_contract.payload_violations(
        {"fn": "invalidate_user_snap", "args": [None]}, "event.user.updated"
    )
    assert any("类型不符" in p for p in problems), problems


def test_optional_tail_allows_shorter_arg_list() -> None:
    """可选尾部：``record_audit_event`` 的 detail 可省，但不得越过上限。"""
    assert (
        event_contract.payload_violations(
            {"fn": "record_audit_event", "args": ["u", "audit.login_fail"]},
            "audit.login_fail",
        )
        == []
    )
    problems = event_contract.payload_violations(
        {"fn": "record_audit_event", "args": ["u", "a", "d", "extra"]},
        "audit.login_fail",
    )
    assert any("实参数过多" in p for p in problems), problems


def test_violation_label_is_bounded() -> None:
    """指标 fn 维度只认登记过的名字，其余归 <unknown>（label 无界 = 基数爆炸）。"""
    assert event_contract.violation_label("notify_upload") == "notify_upload"
    assert event_contract.violation_label("rogue") == "<unknown>"
    assert event_contract.violation_label(None) == "<unknown>"
    assert event_contract.violation_label(123) == "<unknown>"


# ───────────────────────── 5) 三条拦截路径 ─────────────────────────


async def test_publish_rejects_violation_and_counts_it() -> None:
    """发布期：违约 → 不发（transport 收不到）、返回 False、记 produce 侧违约指标。"""
    transport = InMemoryTransport()
    messaging.set_transport(transport)
    before = _violations("notify_upload", "produce")

    ok = await messaging.publish(
        messaging.RKEY_NOTIFY, {"fn": "notify_upload", "args": [123]}
    )

    assert ok is False
    assert transport.published == []
    assert _violations("notify_upload", "produce") == before + 1


async def test_publish_unknown_fn_counts_under_unknown_label() -> None:
    transport = InMemoryTransport()
    messaging.set_transport(transport)
    before = _violations("<unknown>", "produce")

    assert (
        await messaging.publish(messaging.RKEY_NOTIFY, {"fn": "rogue", "args": []})
        is False
    )
    assert _violations("<unknown>", "produce") == before + 1


def test_permanent_failure_reason_flags_contract_violation() -> None:
    """relay 侧：违约判永久失败（一次折叠进 event_failures，不空耗 5 次退避重试）。"""
    reason = messaging.permanent_failure_reason(
        messaging.RKEY_NOTIFY, {"fn": "notify_upload", "args": [123]}
    )
    assert reason is not None and "event contract violation" in reason
    # 合规事件不得被误判（否则正常事件会被折叠归档）
    assert (
        messaging.permanent_failure_reason(
            messaging.RKEY_NOTIFY,
            {"fn": "notify_upload", "args": ["u1"], "event_id": "e"},
        )
        is None
    )


async def test_worker_drops_violating_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """消费期：违约消息 ack 丢弃（不重投），并记 consume 侧违约指标。"""
    captured: dict[str, Any] = {}
    called: list[Any] = []

    async def _spy(*args: Any) -> None:
        called.append(args)

    async def _fake_run_subscription(name: str, handler: Any) -> None:
        captured["handler"] = handler

    monkeypatch.setattr(worker.messaging, "run_subscription", _fake_run_subscription)
    monkeypatch.setattr(worker.metrics_relay, "start_publisher", lambda: None)
    monkeypatch.setattr(worker, "setup_tracing", lambda **_: None)
    # handlers_for 在 _consume 里被取**副本**，故必须在 _consume 之前替换掉它，
    # 否则拿到的是真实 handler（files.tasks.notify_upload，会去连库）
    monkeypatch.setattr(
        worker.task_registry, "handlers_for", lambda _name: {"notify_upload": _spy}
    )

    await worker._consume(worker.NOTIFY_SUBSCRIPTION)
    handler = captured["handler"]

    meta = messaging.MessageMeta(
        topic=messaging.TOPIC_NOTIFY,
        subscription="notify",
        properties={"routing_key": messaging.RKEY_NOTIFY},
    )
    before = _violations("notify_upload", "consume")

    await handler({"fn": "notify_upload", "args": [123]}, meta)

    assert called == [], "违约消息不得进入 handler"
    assert _violations("notify_upload", "consume") == before + 1

    # 合规则正常分派（证明上面的「丢弃」不是因为 handler 压根调不到）
    await handler({"fn": "notify_upload", "args": ["u1"]}, meta)
    assert called == [("u1",)]
