"""可观测基座 · Prometheus metrics（M0.5.1 端点/自动埋点；M0.5.2 业务计数定义）。

与 sentry(apm.py) 平行的本体可观测接入：给 FastAPI 加自动 HTTP 埋点并暴露 /metrics
抓取端点。语义同 Sentry：

- 默认开启（本地无副作用收集器，成本极低）；`LKM_METRICS_ENABLED=false` 可整体关闭；
  关闭/挂载失败一律 fail-open（仅记日志，不阻塞应用启动）。
- 幂等：同一 app 重复装配不重复注册（prometheus_client 用全局默认 REGISTRY，同名 metric
  重复注册会抛 ValueError）。
- **依赖口径**：`prometheus_client` 是硬依赖——下面的指标对象在 import 期就登记到全局
  REGISTRY，缺它会让 import `app.core.metrics`（进而 `app.main`）失败；fail-open 只覆盖
  `/metrics` 与自动埋点的**挂载**（prometheus_fastapi_instrumentator 延迟 import）。
"""

import logging

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---- M0.5.2 业务指标占位 ----
# prometheus_client 全局默认 REGISTRY 唯一注册，故计数器建在模块级单例，消费方
# import 引用即可，避免重复 `Counter(...)` 撞同名 (prometheus 对同名二次注册抛 ValueError)。
# metrics_enabled=false 仅隐藏 /metrics 导出，计数本身照常累计（成本可忽略）。
post_created_total = Counter(
    "post_created_total",
    "全内容产出：统一 content_items / 专栏原生发帖成功落库后 +1（label content_type）",
    ("content_type",),
)
# 消息总线投递失败（publish 抛错 / 不可用），供错误率看板；未配置 broker 属 fail-open
# 不计。由 messaging.publish 唯一计数：outbox relay 经同一 publish 投递，其抛出路径已
# 被此处捕获，relay 不再重复 inc（防同一异常 double-count）。
notify_failed_total = Counter(
    "notify_failed_total",
    "消息总线投递失败次数（publish 抛错 / 不可用，unified at messaging.publish）",
)
# outbox 积压量：outbox relay 每轮 poll 结束后统计仍是 pending（含指数退避等待下一轮）的
# 事件数 set 到此，供积压看板；未配置消息总线时 relay 空转不调用，本 gauge 保持初始 0。
outbox_pending_count = Gauge(
    "outbox_pending_count",
    "outbox_events 中 status=pending 的积压事件数（relay 每轮末尾上报）",
)
# Pulsar 各订阅 lag（msgBacklog）：由 API 进程的 lag 上报器（app/core/pulsar_lag.py）
# 周期从 Pulsar Admin REST 拉取后 set，供「某订阅故障/消费滞后」隔离看板。worker 进程
# 不暴露 /metrics，故只在 API 进程上报。标签 = (subscription, topic)。
pulsar_subscription_backlog = Gauge(
    "pulsar_subscription_backlog",
    "Pulsar 订阅积压消息数 msgBacklog（API 进程周期上报）",
    ("subscription", "topic"),
)

# user:snap 双级缓存（L1 本地 / L2 Redis）命中与未命中；供 roadmap §7.2「启用前后命中率
# /延迟对比」取数。label: layer=l1|l2, result=hit|miss。
user_snap_cache_total = Counter(
    "user_snap_cache_total",
    "user:snap 双级缓存命中/未命中（layer=l1|l2, result=hit|miss）",
    ("layer", "result"),
)
# user:snap 读请求合并（singleflight）：role=leader 为真正执行加载的请求，shared 为复用其结果者。
# leader/shared 比值反映合并收益（趋近 1:1 表示热点击穿被有效收敛）。
user_snap_singleflight_total = Counter(
    "user_snap_singleflight_total",
    "user:snap singleflight 请求合并（role=leader|shared）",
    ("role",),
)


# GraphQL 查询耗时（M6.4）：从 operation 开始到执行收束（含解析/校验/执行），供只读端点
# 的性能看板；被防护拒绝的查询同样计入（耗时短，正是防护生效的形态）。
graphql_query_duration_seconds = Histogram(
    "graphql_query_duration_seconds",
    "GraphQL 单次操作耗时（秒，含解析/校验/执行；M6.4）",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
# GraphQL 被防护拒绝的次数：label reason=depth（深度超限）/complexity（文档规模超限）/
# timeout（时间预算耗尽）。只由防护产生的错误计入——业务 resolver 自身的执行错误不计。
graphql_query_rejected_total = Counter(
    "graphql_query_rejected_total",
    "GraphQL 查询被防护拒绝次数（reason=depth|complexity|timeout；M6.4）",
    ("reason",),
)


def setup_metrics(app: FastAPI) -> None:
    """按 settings 装配 /metrics + 自动 HTTP 埋点；关闭或挂载失败均 fail-open（幂等）。"""
    if not settings.metrics_enabled:
        logger.info("Prometheus metrics 已关闭（LKM_METRICS_ENABLED=false）")
        return
    # 幂等按 app 判定：不能用模块级标志（测试会多次 create_app，每个 app 都需要自己的
    # /metrics 与埋点），只有对**同一个 app** 重复调用才该短路，否则会重复挂 instrumentator
    # 中间件并重复注册同名路由
    if any(
        getattr(route, "path", None) == settings.metrics_endpoint
        for route in app.routes
    ):
        return
    try:
        # 延迟 import 的是 instrumentator（可选挂载件）；prometheus_client 本身是硬依赖，
        # 见模块顶部指标对象在 import 期就注册（此处不再声称能缺件降级）
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(
            app,
            endpoint=settings.metrics_endpoint,
            include_in_schema=False,
            tags=["monitoring"],
        )
        logger.info("Prometheus metrics 已挂载 %s", settings.metrics_endpoint)
    except Exception:
        logger.exception("Prometheus metrics 挂载失败，降级为不加载（fail-open）")
