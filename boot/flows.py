"""Prefect flow 入口模块：装配后重导出各 flow 对象。

process 型 work pool 按 deployment 的 entrypoint 字符串在 worker 容器内 import 模块，
因此 entrypoint 必须指向**本模块**——flow 内部会经 ``core.ports`` 取能力（端口在
``assemble()`` 里绑定），直接指向 ``app/flows/*.py`` 会缺装配。
"""

from __future__ import annotations

from boot.assemble import assemble

assemble()

from app.flows.analytics import analytics_export_flow  # noqa: E402
from app.flows.cron import cron_dispatch_flow  # noqa: E402
from app.flows.feed_backfill import feed_backfill_flow  # noqa: E402
from app.flows.ops_daily import ops_daily_flow  # noqa: E402
from app.flows.search_reindex import search_reindex_flow  # noqa: E402
from app.flows.user_dim import user_dim_reconcile_flow  # noqa: E402

__all__ = [
    "analytics_export_flow",
    "cron_dispatch_flow",
    "feed_backfill_flow",
    "ops_daily_flow",
    "search_reindex_flow",
    "user_dim_reconcile_flow",
]
