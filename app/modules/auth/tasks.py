"""auth 模块队列任务：验证码 / 魔法链接发送 + 用户快照失效（worker 侧执行）。

channels/deps 只在函数内延迟 import，避免 worker 冷启动时经 send→deps→
providers/security→db 拉整棵 auth 树，缩短 worker 启动路径。

任务经 ``register_task`` 注册（§6.2），worker 启动时导入本模块即触发注册，
worker.py 不再手写 handler 表。

- 发送类（send_code / send_magic_link）注册到 send 订阅（send worker 进程消费）。
- 快照失效类（invalidate_user_snap）注册到 user-invalidate 订阅（jobs/default worker
  并行消费），绑定 auth 变更事件；consumer 退化到 ``core.user_cache.invalidate_user_snap``
  = del + epoch bump 的反陈旧失效原语（A6/A7），天然幂等，绝不在失效侧写缓存值。
- **B0.2 事件主路**：消费 auth 变更事件时，「失效在线缓存」的同时按单 user 刷新离线宽表
  ``user_dim``（``user_dim_sync.refresh_user_dim_event``）。它自开独立会话、天然幂等、
  正常跑；异常时**失效先于一切已完成**，dim 刷新仅记日志放行（离线副本可滞后一点，由
  R 侧周期增量对账 ``reconcile_user_dim`` 兜回）——绝不让 ETL 的临时故障反过来影响在线
  失效语义（B0.2 离线写，永不作在线热路径阻塞点）。
- **周期增量对账（crash-safety 网）**：``reconcile_user_dim`` 经 cron 定时发布到
  ``system/cron`` topic，由 jobs 订阅消费（低频，见 register_cron_job），批扫 + 批量 upsert。
"""

import logging

from app.core.messaging import RKEY_RECONCILE, SUB_JOBS, SUB_SEND, SUB_USER_INVALIDATE
from app.core.task_registry import register_cron_job, register_task

logger = logging.getLogger("lkm.auth.tasks")


async def send_code(channel_key: str, contact: str, code: str) -> None:
    """发送验证码。失败抛异常触发重试。"""
    from app.modules.auth import channels as _channels

    await _channels.CHANNELS[channel_key].send_code(contact, code)


async def send_magic_link(email: str, link: str) -> None:
    """发送魔法链接。失败抛异常触发重试。"""
    from app.modules.auth import deps as _deps

    await _deps.get_email_provider().send_magic_link(email, link)


async def invalidate_user_snap(user_id: int) -> None:
    """任务：失效单用户快照（A7/A6 调用口）——del snap + epoch bump，反陈旧复活。

    handler 签名 = 事件 payload["args"] 的命名形参（worker 按名/位置展开调用）。天然幂等：
    对不存在/已失效的 user 也只是 INCR epoch (+DEL no-op)，无双重副作用；绝不在失效侧
    写缓存值（读靠下一回 miss 由 DB 回填）。

    **B0.2**：失效完成后顺带按单 user 刷新离线宽表 ``user_dim``（同一 user.* 变更既打空
    在线缓存、也把最新源摊进离线副本——两者源自同一事件）。dim 刷新自开独立会话、幂等；
    其异常被吞（仅记日志）先保证在线失效/worker 成功不因 ETL 故障受影响，漏刷新由周期
    对账 reconcile 兜底。
    """
    from app.core import user_cache

    await user_cache.invalidate_user_snap(user_id)
    try:
        from app.modules.auth.user_dim_sync import refresh_user_dim_event

        await refresh_user_dim_event(user_id)
    except Exception:
        # B0.2 离线写，fail-open：绝不让 dim ETL 故障反过来影响在线失效语义
        logger.exception("user_dim 事件刷新失败(在线失效已完成) user_id=%s", user_id)


async def reconcile_user_dim() -> None:
    """周期增量对账消费口（jobs worker 消费 cron.reconcile）：批扫 + 批量 upsert。

    依赖函数级 import，避免 worker 冷启动拉整棵 auth/db 树——到点才真正建会话。fn 名与
    register_cron_job 成对声明（见下），scheduler 发布 ``fn=reconcile_user_dim`` 时
    worker 按其名命中本 handler。
    """
    from app.modules.auth.user_dim_sync import reconcile_user_dim_periodic

    await reconcile_user_dim_periodic()


register_task(SUB_SEND.name, "send_code", send_code)
register_task(SUB_SEND.name, "send_magic_link", send_magic_link)
register_task(SUB_USER_INVALIDATE.name, "invalidate_user_snap", invalidate_user_snap)
register_task(SUB_JOBS.name, "reconcile_user_dim", reconcile_user_dim)
# 低频 crash-safety 网：周期增量对账（非新鲜度主路；主路是上面的 user.* 事件）。每日 03:10
# 由 scheduler 发布 cron.reconcile→jobs 订阅。routing/cron 复用既有 cron.reconcile 键/订阅。
register_cron_job(
    job_id="reconcile_user_dim",
    cron="10 3 * * *",  # 每日 03:10
    routing_key=RKEY_RECONCILE,
    fn="reconcile_user_dim",
)
