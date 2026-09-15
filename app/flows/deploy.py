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
]


def main() -> None:
    for flow_obj, name, entrypoint in DEPLOYMENTS:
        deployment_id = flow_obj.from_source(
            source=SOURCE, entrypoint=entrypoint
        ).deploy(
            name=name,
            work_pool_name=WORK_POOL,
            build=False,
            push=False,
        )
        logger.info(
            "已注册 deployment: %s/%s (id=%s)",
            flow_obj.name,
            name,
            deployment_id,
        )


if __name__ == "__main__":
    main()
