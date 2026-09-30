"""事件契约：``fn`` → 参数形状 + 承载它的逻辑 routing_key（**唯一事实源**）。

为什么契约必须在应用层
--------------------
事件 envelope 是通用的 ``{fn, args, event_id}``，而 **broker 侧的 schema registry 不校验
payload**。2026-09-30 在真 broker（standalone 3.3.0）上实测：

- 开 ``schemaValidationEnforced=true``（命名空间 ``set-schema-validation-enforce -e``）后，
  违约 payload（``fn`` 为整数 / ``args`` 为字符串 / 整条不成形）**照样投递成功**；
- 把 ``fn`` 收紧成 Avro ``enum`` 后，发送**未声明**的 fn 也照样成功；
- 该开关真正生效的只有一条：**topic 已有 schema 时，未声明 schema 的 producer 接不进来**
  （``IncompatibleSchema``）；且 topic 尚无 schema 时无 schema 的 producer 反而能抢先建出
  无 schema 的 topic。
- 消费者不带 schema 也不受影响。

即 broker 层最多保证「谁在发」，**保证不了「发的是什么」**——只开那个开关就是一句虚假保证。
（部署侧该开关仍开，见 ``deploy/pulsar/entrypoint.sh``；其确切语义写进 DEPLOYMENT.md。）

因此「事件契约」= 本模块的声明式登记表 + 三处校验点：

1. **装配期**（``task_registry.register_task`` / ``register_cron_job``）：handler 的形参个数
   必须与契约的必填/总个数相容——写错在进程启动时即炸，而不是等某条消息到了才 TypeError。
   （worker 用 ``handler(*args)`` **按位置**展开，故校验的是元数而非形参名；同名 fn 的多个
   handler 形参名本就不一致，如 ``apply_point_event`` 在 points 侧叫 ``user_id``、在
   notification 侧叫 ``actor_id``。）
2. **发布期**（``messaging.publish``，全仓唯一咽喉）：违约 → ERROR 日志 + 违约指标 + 返回
   False；``messaging.permanent_failure_reason`` 同步把违约判为**永久失败**，使 relay 把它
   折叠进 ``event_failures``（可人工重放）而不是重试 5 次再丢。
3. **消费期**（``worker._on_payload``）：违约消息 ack 丢弃并计违约指标（确定性坏消息重投无益）。

**参数类型按「线上 JSON 形态」声明**，不按 Python 标注：``uuid.UUID`` 过 JSON 后是 str，
``datetime`` 同理；handler 上的类型标注表达的是意图（且本仓并不一致），不能当契约。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.core.messaging import (
    RKEY_ANALYTICS,
    RKEY_AUDIT_LOGIN_FAIL,
    RKEY_AUDIT_PERMISSION_CHANGE,
    RKEY_BLOOM_SEED,
    RKEY_CLEANUP,
    RKEY_CONTENT_DELETED,
    RKEY_CONTENT_PUBLISHED,
    RKEY_CONTENT_UPDATED,
    RKEY_NOTIFY,
    RKEY_OPS_DAILY,
    RKEY_POINTS,
    RKEY_RECONCILE,
    RKEY_SEND_CODE,
    RKEY_SEND_MAGIC,
    RKEY_USER_BANNED,
    RKEY_USER_SESSION_REVOKE,
    RKEY_USER_UPDATED,
    ROUTING_KEY_TOPICS,
)

logger = logging.getLogger("lkm.event_contract")

# envelope 允许出现的键（多一个都算违约：``enqueue_outbox`` 的 payload 是 {fn,args}，
# relay 再注入 event_id）。历史上曾靠「多余键对 handler 无害」放行，这里收紧。
_ENVELOPE_KEYS: Final = frozenset({"fn", "args", "event_id"})

# 线上 JSON 形态的类型字面量（json.loads 之后的确切类型）
_WIRE_KINDS: Final = frozenset({"str", "int", "float", "bool", "list", "dict"})


@dataclass(frozen=True)
class Arg:
    """契约里的一个位置实参（描述**线上**形态）。

    ``optional`` 只允许出现在尾部（模块导入期自检），表示发布方可以不带该实参（对应
    handler 上的默认值）。
    """

    name: str
    kind: str
    nullable: bool = False
    optional: bool = False


@dataclass(frozen=True)
class EventContract:
    """一个 ``fn`` 的事件契约。

    ``routing_keys``：允许承载该 fn 的逻辑键（发布方**必须**其中之一，装配期校验 cron 声明）。
    ``note``：谁在发、谁在收，供审计时人读。
    """

    fn: str
    routing_keys: tuple[str, ...]
    args: tuple[Arg, ...] = ()
    note: str = ""

    @property
    def required(self) -> int:
        """必填实参个数（前 N 个）。"""
        return sum(1 for a in self.args if not a.optional)

    @property
    def maximum(self) -> int:
        """实参个数上限。"""
        return len(self.args)


def _cron(fn: str, routing_key: str) -> EventContract:
    """cron 任务契约：无实参（scheduler 发 ``{"fn": fn}``，见 core/scheduler.py）。"""
    return EventContract(
        fn,
        (routing_key,),
        (),
        note=f"cron：scheduler → jobs 订阅 {fn}",
    )


# ---- 契约登记表（唯一事实源；新增/改动事件必须同时改这里）----
EVENT_CONTRACTS: dict[str, EventContract] = {
    c.fn: c
    for c in (
        # ---- auth 进程发布 ----
        EventContract(
            "send_code",
            (RKEY_SEND_CODE,),
            (Arg("channel_key", "str"), Arg("contact", "str"), Arg("code", "str")),
            note="jobs.send_code / auth 登录验证码 → send 订阅",
        ),
        EventContract(
            "send_magic_link",
            (RKEY_SEND_MAGIC,),
            (Arg("email", "str"), Arg("link", "str")),
            note="jobs.send_magic_link → send 订阅",
        ),
        EventContract(
            "invalidate_user_snap",
            (RKEY_USER_UPDATED, RKEY_USER_BANNED, RKEY_USER_SESSION_REVOKE),
            (Arg("user_id", "str"),),
            note="auth/events.py（三个发射口共用一个 fn）→ user-invalidate 订阅",
        ),
        EventContract(
            "record_audit_event",
            (RKEY_AUDIT_LOGIN_FAIL, RKEY_AUDIT_PERMISSION_CHANGE),
            (
                Arg("user_id", "str", nullable=True),
                Arg("action", "str"),
                Arg("detail", "str", optional=True),
            ),
            note="auth/events.py 审计家族 → audit / audit-permission 订阅（§5.2）",
        ),
        # ---- 业务事件 ----
        EventContract(
            "notify_upload",
            (RKEY_NOTIFY,),
            (Arg("upload_id", "str"),),
            note="files.notify 上传登记 → notify 订阅（缩图）",
        ),
        EventContract(
            "apply_point_event",
            (RKEY_POINTS,),
            (Arg("user_id", "str"), Arg("event", "str"), Arg("ref_id", "str")),
            note="points.rules → points-reward/stats/tasks + notification 四订阅扇出",
        ),
        EventContract(
            "apply_content_event",
            (
                RKEY_CONTENT_PUBLISHED,
                RKEY_CONTENT_UPDATED,
                RKEY_CONTENT_DELETED,
            ),
            (Arg("item_id", "str"), Arg("action", "str")),
            note="content.events → content-index 订阅（外部检索增量同步）",
        ),
        # ---- cron（scheduler 发布，无实参）----
        _cron("cleanup_expired_uploads", RKEY_CLEANUP),
        _cron("flush_content_counters", RKEY_CLEANUP),
        _cron("purge_revoked_access_tokens", RKEY_CLEANUP),
        _cron("purge_stale_view_logs", RKEY_CLEANUP),
        _cron("fanout_feed_items", RKEY_RECONCILE),
        _cron("reconcile_blog_repos", RKEY_RECONCILE),
        _cron("reconcile_content_counts", RKEY_RECONCILE),
        _cron("reconcile_content_counts_full", RKEY_RECONCILE),
        _cron("reconcile_user_dim", RKEY_RECONCILE),
        _cron("export_analytics_clickhouse", RKEY_ANALYTICS),
        _cron("run_ops_daily", RKEY_OPS_DAILY),
        _cron("seed_user_id_bloom", RKEY_BLOOM_SEED),
    )
}


def _validate_registry(contracts: dict[str, EventContract] | None = None) -> None:
    """装配期自检登记表（拼错的 kind / 可选项不在尾部 / 未知 routing_key 在 import 期即炸）。

    ``contracts`` 仅为可测试性留口（默认校验本模块的 :data:`EVENT_CONTRACTS`）。
    """
    for fn, contract in (EVENT_CONTRACTS if contracts is None else contracts).items():
        if fn != contract.fn:
            raise ValueError(f"契约登记表键 {fn!r} 与契约 fn {contract.fn!r} 不一致")
        if not contract.routing_keys:
            raise ValueError(f"fn={fn!r} 未声明承载它的 routing_key")
        for routing_key in contract.routing_keys:
            if routing_key not in ROUTING_KEY_TOPICS:
                raise ValueError(f"fn={fn!r} 引用了未知 routing_key {routing_key!r}")
        seen_optional = False
        for arg in contract.args:
            if arg.kind not in _WIRE_KINDS:
                raise ValueError(f"fn={fn!r} 实参 {arg.name!r} 类型未知: {arg.kind!r}")
            if arg.optional:
                seen_optional = True
            elif seen_optional:
                raise ValueError(f"fn={fn!r} 必填实参 {arg.name!r} 出现在可选项之后")


_validate_registry()


# ---- 校验 ----


def _value_matches(value: Any, arg: Arg) -> bool:
    if value is None:
        return arg.nullable
    kind = arg.kind
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        # bool 是 int 的子类，但线上 True/1 语义完全不同，必须排除
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "list":
        return isinstance(value, list)
    return isinstance(
        value, dict
    )  # kind == "dict"（_validate_registry 已保证取值合法）


def payload_violations(
    payload: Mapping[str, Any] | Any, routing_key: str | None = None
) -> list[str]:
    """校验一条**线上形态**（json.loads 之后）的 envelope；返回违约原因清单，空=合规。

    ``routing_key`` 给定时连带校验「该 fn 确实允许经这个逻辑键承载」；消费侧拿不到
    properties 时可传 None 只校验 fn/args。
    """
    if not isinstance(payload, Mapping):
        return [f"payload 非对象（{type(payload).__name__}）"]

    problems: list[str] = []
    extra = set(payload) - _ENVELOPE_KEYS
    if extra:
        problems.append(f"未知 envelope 键 {sorted(extra)}")

    fn = payload.get("fn")
    if not isinstance(fn, str) or not fn:
        problems.append(f"fn 缺失或非字符串（{type(fn).__name__}）")
        return problems

    contract = EVENT_CONTRACTS.get(fn)
    if contract is None:
        problems.append(f"未登记的事件 fn={fn!r}")
        return problems

    if routing_key is not None and routing_key not in contract.routing_keys:
        problems.append(
            f"fn={fn} 不得经 {routing_key} 承载（契约允许 {list(contract.routing_keys)}）"
        )

    args = payload.get("args", [])
    if not isinstance(args, list):
        problems.append(f"args 非数组（{type(args).__name__}）")
        return problems

    if len(args) < contract.required:
        problems.append(
            f"实参数不足：{len(args)} < {contract.required}（{fn} 需要 "
            f"{[a.name for a in contract.args]}）"
        )
    elif len(args) > contract.maximum:
        problems.append(
            f"实参数过多：{len(args)} > {contract.maximum}（{fn} 最多 "
            f"{[a.name for a in contract.args]}）"
        )
    else:
        for index, (value, arg) in enumerate(zip(args, contract.args, strict=False)):
            if not _value_matches(value, arg):
                problems.append(
                    f"实参#{index} {arg.name} 类型不符：期望 "
                    f"{arg.kind}{'(nullable)' if arg.nullable else ''}，"
                    f"实际 {type(value).__name__}"
                )

    event_id = payload.get("event_id")
    if event_id is not None and not isinstance(event_id, str):
        problems.append(f"event_id 非字符串（{type(event_id).__name__}）")

    return problems


def violation_label(fn: Any) -> str:
    """违约指标的 ``fn`` 维度：只认登记表里的名字，其余归 ``<unknown>``（防 label 基数爆炸）。"""
    return fn if isinstance(fn, str) and fn in EVENT_CONTRACTS else "<unknown>"


def record_violation(
    fn: Any, side: str, problems: Sequence[str], *, where: str = ""
) -> None:
    """记一次契约违约：计数 + ERROR 日志（发布/消费两侧共用，保证口径一致）。"""
    from app.core.metrics import event_contract_violations_total

    event_contract_violations_total.labels(fn=violation_label(fn), side=side).inc()
    logger.error(
        "事件契约违约 side=%s fn=%s%s: %s",
        side,
        fn,
        f" {where}" if where else "",
        "; ".join(problems),
    )


def handler_violation(fn: str, handler: Callable[..., Any]) -> str | None:
    """校验 handler 的形参元数与契约相容；返回违约原因，None=相容。

    相容条件（见模块头：dispatch 是 ``handler(*args)`` 按位置展开，故只校验元数）：
    ``handler 必填形参数 <= 契约必填实参数`` 且 ``handler 位置形参总数 >= 契约实参数上限``
    ——这样契约允许的任何一种实参个数，handler 都接得住。
    """
    contract = EVENT_CONTRACTS.get(fn)
    if contract is None:
        return f"任务 {fn!r} 未登记事件契约（core/event_contract.EVENT_CONTRACTS）"

    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError) as exc:  # 极少数内建/C 扩展 callable 拿不到签名
        return f"任务 {fn!r} 的 handler 无法取签名：{exc}"

    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return None  # *args：任意元数都接得住

    positional = [
        p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in positional if p.default is inspect.Parameter.empty]

    if len(required) > contract.required:
        return (
            f"任务 {fn!r} 的 handler 必填形参 {len(required)} 个 "
            f"多于契约必填实参 {contract.required} 个"
        )
    if len(positional) < contract.maximum:
        return (
            f"任务 {fn!r} 的 handler 位置形参只有 {len(positional)} 个，"
            f"接不住契约最多 {contract.maximum} 个实参"
        )
    return None
