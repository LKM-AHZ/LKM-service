"""注册 Prefect deployment（prefect-worker 每次启动时调用）。

cron 任务也注册为 Prefect deployment；deployment 在 server 就绪后注册一次即可。重复执行同名
deployment 会更新而非报错，故该服务可安全重跑。process 型 work pool 直接在 prefect-worker
容器内以本地源码执行 flow，不需要构建/推送镜像（build/push=False）。

注册清单列表驱动：新增 flow 只需在 ``DEPLOYMENTS`` 追加一行（flow 对象 / deployment 名 /
本地 entrypoint），无需改 main 逻辑。

**entrypoint 一律指向 ``boot/flows.py``**（其模块导入即装配）：flow 内部经 ``core.ports``
取能力，直接指向 ``app/flows/*.py`` 会因缺装配而取不到实现。
"""

from __future__ import annotations

import logging
from typing import Any

from app.flows.analytics import analytics_export_flow
from app.flows.cron import cron_dispatch_flow
from app.flows.feed_backfill import feed_backfill_flow
from app.flows.ops_daily import ops_daily_flow
from app.flows.search_reindex import search_reindex_flow
from app.flows.user_dim import user_dim_reconcile_flow
from boot.assemble import assemble
from core.config import settings
from core.task_registry import cron_jobs, ensure_tasks_registered

logger = logging.getLogger("lkm.flows.deploy")

WORK_POOL = settings.prefect_work_pool
# process 型 pool 不支持自定义镜像：deployment 用**本地源码路径**注册，worker 容器内
# 直接以该路径执行（镜像即 lkm-service:latest，无需构建/推送）。
SOURCE = settings.prefect_source

# (flow 对象, deployment 名, entrypoint)
# entrypoint 与 user_dim 以外的 deployment 名**刻意作为常量**：部署侧从未下发过对应 env，
# 做成可配置只会多出没有真实自由度的配置项（与「无自由度不加配置」的既有取向一致）。
DEPLOYMENTS: list[tuple[Any, str, str]] = [
    (
        user_dim_reconcile_flow,
        settings.prefect_flow_deployment_name,
        "boot/flows.py:user_dim_reconcile_flow",
    ),
    (
        analytics_export_flow,
        "analytics-export",
        "boot/flows.py:analytics_export_flow",
    ),
    (
        search_reindex_flow,
        "search-reindex",
        "boot/flows.py:search_reindex_flow",
    ),
    (
        feed_backfill_flow,
        "feed-backfill",
        "boot/flows.py:feed_backfill_flow",
    ),
    (
        ops_daily_flow,
        "ops-daily",
        "boot/flows.py:ops_daily_flow",
    ),
]


def _check_entrypoint(flow_obj: Any, entrypoint: str) -> None:
    """注册前校验 entrypoint 指向的函数就是被注册的那个 flow。

    两者都可被环境变量独立覆盖：把 ``LKM_PREFECT_ENTRYPOINT`` 指到别的函数，
    deployment 实际执行的 flow 就与日志里 ``flow_obj.name`` 声称的不是同一个，
    且不会有任何报错——只能等跑起来才发现。比较用 ``flow_obj.fn.__name__``：
    这些 flow 的 ``@flow(name=...)`` 是展示名（如 user-dim-reconcile），与函数名不同。
    """
    _, _, func_name = entrypoint.rpartition(":")
    fn = getattr(flow_obj, "fn", None)
    expected = getattr(fn, "__name__", None)
    if expected is not None and func_name != expected:
        raise ValueError(
            f"entrypoint {entrypoint!r} 指向 {func_name!r}，"
            f"与被注册的 flow 函数 {expected!r} 不一致"
        )


def main() -> None:
    failures: list[str] = []
    assemble()
    ensure_tasks_registered()
    deployments = [(*deployment, None) for deployment in DEPLOYMENTS]
    deployments.extend(
        (
            cron_dispatch_flow,
            job["id"],
            "boot/flows.py:cron_dispatch_flow",
            job,
        )
        for job in cron_jobs()
    )
    for flow_obj, name, entrypoint, cron_job in deployments:
        try:
            _check_entrypoint(flow_obj, entrypoint)
            options = {}
            if cron_job is not None:
                options = {
                    "cron": cron_job["cron"],
                    "parameters": {"job_id": cron_job["id"]},
                    "concurrency_limit": 1,
                    "paused": not cron_job["enabled"],
                }
            deployment_id = flow_obj.from_source(
                source=SOURCE, entrypoint=entrypoint
            ).deploy(
                name=name,
                work_pool_name=WORK_POOL,
                build=False,
                push=False,
                **options,
            )
        except Exception as exc:
            logger.exception("注册 deployment 失败: %s (%s)", name, exc)
            failures.append(name)
            continue
        logger.info(
            "已注册 deployment: %s/%s (id=%s)",
            flow_obj.name,
            name,
            deployment_id,
        )
    if failures:
        raise RuntimeError(f"以下 deployment 注册失败：{', '.join(failures)}")


if __name__ == "__main__":
    main()
