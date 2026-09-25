"""注册 Prefect deployment（供 ``prefect-init`` 一次性服务调用）。

APScheduler 只做简单 cron 触发入口；deployment 在 server 就绪后注册一次即可。重复执行同名
deployment 会更新而非报错，故该服务可安全重跑。process 型 work pool 直接在 prefect-worker
容器内以本地源码执行 flow，不需要构建/推送镜像（build/push=False）。

注册清单列表驱动：新增 flow 只需在 ``DEPLOYMENTS`` 追加一行（flow 对象 / deployment 名 /
本地 entrypoint），无需改 main 逻辑。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.flows.analytics import analytics_export_flow
from app.flows.feed_backfill import feed_backfill_flow
from app.flows.search_reindex import search_reindex_flow
from app.flows.user_dim import user_dim_reconcile_flow

logger = logging.getLogger("lkm.flows.deploy")

WORK_POOL = os.getenv("LKM_PREFECT_WORK_POOL", "lkm")
# process 型 pool 不支持自定义镜像：deployment 用**本地源码路径**注册，worker 容器内
# 直接以该路径执行（镜像即 lkm-service:latest，无需构建/推送）。
SOURCE = os.getenv("LKM_PREFECT_SOURCE", "/app")

# (flow 对象, deployment 名, entrypoint)
DEPLOYMENTS: list[tuple[Any, str, str]] = [
    (
        user_dim_reconcile_flow,
        os.getenv("LKM_PREFECT_FLOW_DEPLOYMENT_NAME", "reconcile"),
        os.getenv(
            "LKM_PREFECT_ENTRYPOINT", "app/flows/user_dim.py:user_dim_reconcile_flow"
        ),
    ),
    (
        analytics_export_flow,
        os.getenv("LKM_PREFECT_ANALYTICS_DEPLOYMENT_NAME", "analytics-export"),
        os.getenv(
            "LKM_PREFECT_ANALYTICS_ENTRYPOINT",
            "app/flows/analytics.py:analytics_export_flow",
        ),
    ),
    (
        search_reindex_flow,
        os.getenv("LKM_PREFECT_SEARCH_DEPLOYMENT_NAME", "search-reindex"),
        os.getenv(
            "LKM_PREFECT_SEARCH_ENTRYPOINT",
            "app/flows/search_reindex.py:search_reindex_flow",
        ),
    ),
    (
        feed_backfill_flow,
        os.getenv("LKM_PREFECT_FEED_BACKFILL_DEPLOYMENT_NAME", "feed-backfill"),
        os.getenv(
            "LKM_PREFECT_FEED_BACKFILL_ENTRYPOINT",
            "app/flows/feed_backfill.py:feed_backfill_flow",
        ),
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
    for flow_obj, name, entrypoint in DEPLOYMENTS:
        try:
            _check_entrypoint(flow_obj, entrypoint)
            deployment_id = flow_obj.from_source(
                source=SOURCE, entrypoint=entrypoint
            ).deploy(
                name=name,
                work_pool_name=WORK_POOL,
                build=False,
                push=False,
            )
        except Exception as exc:
            # 逐条隔离：任一 deployment 注册失败不能中止其余（否则后面的 flow 全没注册，
            # 而 APScheduler 的 cron 触发会指向不存在的 deployment）。失败在末尾汇总抛出。
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
