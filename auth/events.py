"""auth 事件入队：User/Profile 快照失效（M3.A/A7）+ 审计事件（§5.2）。

快照 ``user:snap:{id}`` 由 ``core.user_cache`` cache-through 承载，其内容同时依赖
**User**(username/account_level/is_locked) 与 **Profile**(nickname/avatar/role) 两表。
A6 的失效原语只做 ``del + epoch bump``（失效不写缓存值；读靠下一回 DB 回填）。

A7 把「写后失效」接到这里：在每次真实变更加载点发出 outbox 事件，由 worker 消费后在
redis 侧失效，保证下次 ``get_user_snapshot`` 拉到的是 DB 新值、陈旧缓存不复活。

消费侧（``auth.tasks.py``）把三个 user.* routing key 都绑定到同一 ``invalidate_user_snap``
handler：三种事件在快照语义上都只需失效该 user 的缓存，路由键仅作主题可观测性/审计粒度。
``audit.*`` 两个键则由 ``record_audit_event`` 消费成指标与告警。

--- 投递方式：**一律自建业务库会话、独立提交**（``_enqueue_committed``）---

这是 2026-09-26 真机验收修掉的核心缺陷。此前这些发射口把事件行 **join 到调用方传入的
``db`` 会话**（"同事务入队"），但：

``outbox_events`` 属**业务库**表，而 auth 侧请求持有的是 **auth 库**会话
（``get_auth_session``；拆库后 users/profiles 唯属 auth 库）。把业务表行挂在 auth 库会话上，
flush/commit 会打到 ``lkm_auth`` → ``relation "outbox_events" does not exist`` → **整个请求
500**。实测触发点：已存在账号走注册入口（``_login_or_error`` → ``upgrade_to_normal`` →
``notify_user_updated``）；它同时让 ``grant_*`` 与 ``audit.permission_change`` 在拆库拓扑下
全线不可用。此前只在蓝绿/融合单库态侥幸成立，这正是该缺陷长期没暴露的原因。

代价与取舍：事件不再与调用方事务同生共死（调用方回滚时会多出一条"落空"的失效事件）。
本模块**全部发射点都在成功路径上**，故实际差异只是「回滚时多一次缓存失效」——失效本身
幂等且便宜（del + epoch bump），远小于「整条链路 500」。同理，审计事件宁可多记一条，
也不该让登录/授权主流程崩掉。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.messaging import (
    RKEY_AUDIT_LOGIN_FAIL,
    RKEY_AUDIT_PERMISSION_CHANGE,
    RKEY_USER_BANNED,
    RKEY_USER_SESSION_REVOKE,
    RKEY_USER_UPDATED,
)
from app.db.outbox import enqueue_outbox
from app.db.session import new_session

logger = logging.getLogger("lkm.auth.events")

# 全部用户事件共用的 worker 分发名（consumer handler，见 auth.tasks 注册的同名 fn）。
_EVENT_FN = "invalidate_user_snap"

# 审计事件的 worker 分发名（两个 routing key 复用同一 handler，按 args 里的 action 区分）。
_AUDIT_FN = "record_audit_event"


async def _enqueue_committed(
    routing_key: str, payload: dict[str, Any], *, what: str
) -> None:
    """投递一个 auth 事件：自建业务库会话 + 独立提交（理由见模块 docstring）。

    失败只记日志、不外抛（fail-open）：事件不可达不该把登录/授权这类主流程带崩。
    ``what`` 仅用于日志，便于定位是哪个发射口出的问题。
    """
    # 与 `enqueue_outbox` 同一道总开关（未配总线 → 不入队）。在此提前返回是为了**不白开
    # 一个业务库会话**：dev/测试常态下总线是关的，逐个发射点去建连纯属浪费，且会在测试
    # 里无故建起业务库的模块级 engine 单例。
    if not settings.message_bus_enabled:
        return

    db: AsyncSession | None = None
    try:
        # 会话获取也放进 try：new_session 可能因懒建引擎失败（库不可达/配置缺失）而抛，
        # 那同样属于本函数承诺吞掉的失败面。
        db = await new_session()
        await enqueue_outbox(db, routing_key, payload)
        await db.commit()
    except Exception:
        logger.exception("outbox %s own-tx enqueue 失败（事件未投出）", what)
    finally:
        if db is not None:
            await db.close()


async def notify_user_updated(user_id: uuid.UUID) -> None:
    """常规身份变更（profile 编改/头像/升降级等）→ 投递 ``event.user.updated``。"""
    await _enqueue_committed(
        RKEY_USER_UPDATED, {"fn": _EVENT_FN, "args": [user_id]}, what="user.updated"
    )


async def notify_user_session_revoke(user_id: uuid.UUID) -> None:
    """密码重置/全量会话吊销 → 投递 ``event.user.session_revoke``。"""
    await _enqueue_committed(
        RKEY_USER_SESSION_REVOKE,
        {"fn": _EVENT_FN, "args": [user_id]},
        what="user.session_revoke",
    )


async def notify_user_banned(user_id: uuid.UUID) -> None:
    """账户封禁/自动锁定 → 投递 ``event.user.banned``。

    本发射口的特殊之处不在投递方式（上面已统一为独立提交），而在**调用时机**：触达锁定
    阈值时锁定本身经 ``isolated_update`` 的 savepoint 已提交、而外层请求即将回滚——独立提交
    正是这条路径一开始就需要它的原因，如今成了全模块的统一取法。
    """
    await _enqueue_committed(
        RKEY_USER_BANNED, {"fn": _EVENT_FN, "args": [user_id]}, what="user.banned"
    )


async def notify_audit_permission_change(user_id: uuid.UUID, detail: str) -> None:
    """权限/等级变更审计事件 → ``audit.permission_change``。

    ``detail`` 记清是哪一种变更（如 ``grant_exam_unlock``），供消费侧区分与人工追溯。
    """
    await _enqueue_committed(
        RKEY_AUDIT_PERMISSION_CHANGE,
        {"fn": _AUDIT_FN, "args": [str(user_id), RKEY_AUDIT_PERMISSION_CHANGE, detail]},
        what=RKEY_AUDIT_PERMISSION_CHANGE,
    )


async def notify_audit_login_fail(
    user_id: uuid.UUID | None, reason: str, ip_address: str = ""
) -> None:
    """登录失败审计事件 → ``audit.login_fail``。

    ``user_id`` 可为 None（账号不存在/防御用户枚举的虚拟校验分支）；``reason`` 说明失败形态；
    ``ip_address`` 供撞库来源定位。失败登录必然让外层请求抛 ``BizError`` 并回滚，独立提交
    是这条路径从一开始就必须的（现已是全模块统一取法）。
    """
    detail = f"{reason} ip={ip_address}" if ip_address else reason
    await _enqueue_committed(
        RKEY_AUDIT_LOGIN_FAIL,
        {
            "fn": _AUDIT_FN,
            "args": [
                str(user_id) if user_id is not None else None,
                RKEY_AUDIT_LOGIN_FAIL,
                detail,
            ],
        },
        what=RKEY_AUDIT_LOGIN_FAIL,
    )
