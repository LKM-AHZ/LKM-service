"""auth 用户变更失效事件入队（M3.A/A7）：User/Profile 快照相关变更外发。

快照 ``user:snap:{id}`` 由 ``core.user_cache`` cache-through 承载，其内容同时依赖
**User**(username/account_level/is_locked) 与 **Profile**(nickname/avatar/role) 两表。
A6 的失效原语只做 ``del + epoch bump``（失效不写缓存值；读靠下一回 DB 回填）。

A7 把「写后失效」接到这里：在每次真实变更加载点发出 outbox 事件，由 worker 消费后在
redis 侧失效，保证下次 ``get_user_snapshot`` 拉到的是 DB 新值、陈旧缓存不复活。

- **同事务入队**（默认）：``notify_user_updated/notify_user_banned`` 把事件行 join 到传入
  的 ``db`` 会话当前事务，随业务会话的 commit 一并持久——镜像 ``points.rules.enqueue_``
  与 ``files.notify._enqueue_upload`` 的落位约定。未配置消息总线 → ``enqueue_outbox``
  门控直返 False（fail-open，dev/测试不产生积压）。
- **自建会话独立投递**：``notify_user_banned_committed`` 供「锁定本身用 savepoint 隔离提交、
  而外部请求事务将要回滚」的路径使用——此时事件必须独立提交到独立事务才不与锁定错位
  （镜像 ``files.notify._enqueue_upload`` 的 own-session + commit 模式）。Redis 失效
  天然幂等，重放/重复事件无双重副作用；outbox relay 亦按 event_id 去重。

消费侧（``auth.tasks.py``）把这三个 routing key 都绑定到同一 ``invalidate_user_snap``
handler：三种事件在快照语义上都只需失效该 user 的缓存，路由键仅作主题可观测性/审计粒度。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import (
    RKEY_USER_BANNED,
    RKEY_USER_SESSION_REVOKE,
    RKEY_USER_UPDATED,
)
from app.db.outbox import enqueue_outbox
from app.db.session import new_session

logger = logging.getLogger("lkm.auth.events")

# 全部用户事件共用的 worker 分发名（consumer handler，见 auth.tasks 注册的同名 fn）。
_EVENT_FN = "invalidate_user_snap"


async def notify_user_updated(db: AsyncSession, user_id: uuid.UUID) -> None:
    """常规身份变更（profile 编改/头像/升降级等）→ 同事务入队 ``event.user.updated``。"""
    await enqueue_outbox(db, RKEY_USER_UPDATED, {"fn": _EVENT_FN, "args": [user_id]})


async def notify_user_session_revoke(db: AsyncSession, user_id: uuid.UUID) -> None:
    """密码重置/全量会话吊销 → 同事务入队 ``event.user.session_revoke``。"""
    await enqueue_outbox(
        db, RKEY_USER_SESSION_REVOKE, {"fn": _EVENT_FN, "args": [user_id]}
    )


async def notify_user_banned_committed(user_id: uuid.UUID) -> None:
    """账户自动锁定的失效事件，以**独立会话**提交（与 savepoint 隔离的锁定同寿命）。

    失败登录触达锁定阈值时，``login_password`` 会对该请求抛出认证错误并让外层
    ``get_session`` **回滚**，但锁定本身经 ``isolated_update`` 的 savepoint **已提交并保留**。
    若此时把事件行挂在即将回滚的外层事务里，事件会被一并丢弃 → 与已提交的锁定错位。
    故此处自建会话把 ``event.user.banned`` 独立提交，保证「锁定成 → 失效事件必达」。
    Redis 未启用 → ``enqueue_outbox`` 门控直返；异常打日志不阻断锁定语义（fail-open）。
    """
    db = await new_session()
    try:
        await enqueue_outbox(
            db, RKEY_USER_BANNED, {"fn": _EVENT_FN, "args": [user_id]}
        )
        await db.commit()
    except Exception:
        logger.exception("outbox user.banned own-tx enqueue 失败 user_id=%s", user_id)
    finally:
        await db.close()
