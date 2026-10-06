"""
可观测基座 · Prometheus metrics。
- 默认开启（本地无副作用收集器，成本极低）；`LKM_METRICS_ENABLED=false` 可整体关闭；
  关闭/挂载失败一律 fail-open（仅记日志，不阻塞应用启动）。
- 幂等：同一 app 重复装配不重复注册（prometheus_client 用全局默认 REGISTRY，同名 metric
  重复注册会抛 ValueError）。
- **依赖口径**：`prometheus_client` 是硬依赖——下面的指标对象在 import 期就登记到全局
  REGISTRY，缺它会让 import `core.metrics`（进而 `app.main`）失败；fail-open 只覆盖
  `/metrics` 与自动埋点的**挂载**（prometheus_fastapi_instrumentator 延迟 import）。
"""

import logging
from collections.abc import Iterable

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector

from core.config import settings

logger = logging.getLogger(__name__)

post_created_total = Counter(
    "post_created_total",
    "全内容产出：统一 content_items / 专栏原生发帖成功落库后 +1（label content_type）",
    ("content_type",),
)
# content.* 事件消费（content-index 订阅）：外部检索索引增量同步的动作计数。
# label action=published|updated|deleted。消费失败重投超限进 DLQ 时不计入（由 DLQ 侧观测）。
content_index_events_total = Counter(
    "content_index_events_total",
    "content.* 事件消费计数（外部检索索引增量同步，label action）",
    ("action",),
)
# 外部检索失败回落 PG 的次数（label engine）：>0 表示索引面正在降级服务，检索仍可用。
search_engine_fallback_total = Counter(
    "search_engine_fallback_total",
    "外部检索失败回落 PG 的次数（label engine=meilisearch|opensearch）",
    ("engine",),
)
notify_failed_total = Counter(
    "notify_failed_total",
    "消息总线投递失败次数（publish 抛错 / 不可用，unified at messaging.publish）",
)
event_contract_violations_total = Counter(
    "event_contract_violations_total",
    "事件契约违约次数（side=produce|consume；detail 见日志）",
    ("fn", "side"),
)

# outbox 积压量：outbox relay 每轮 poll 结束后统计仍是 pending（含指数退避等待下一轮）的
# 事件数 set 到此，供积压看板；未配置消息总线时 relay 空转不调用，本 gauge 保持初始 0。
outbox_pending_count = Gauge(
    "outbox_pending_count",
    "outbox_events 中 status=pending 的积压事件数（relay 每轮末尾上报）",
)
outbox_leader_total = Counter(
    "outbox_leader_total",
    "outbox relay 领导者选举事件（event=acquired|contended|renew_failed|stale_reclaimed）",
    ("event",),
)
pulsar_subscription_backlog = Gauge(
    "pulsar_subscription_backlog",
    "Pulsar 订阅积压消息数 msgBacklog（API 进程周期上报）",
    ("subscription", "topic"),
)
message_subscription_backlog = Gauge(
    "message_subscription_backlog",
    "所选消息总线的订阅积压（API 进程周期上报）",
    ("broker", "subscription", "topic"),
)

# user:snap 双级缓存（L1 本地 / L2 Redis）命中与未命中；供 roadmap §7.2「启用前后命中率
# /延迟对比」取数。label: layer=l1|l2, result=hit|miss。
user_snap_cache_total = Counter(
    "user_snap_cache_total",
    "user:snap 双级缓存命中/未命中（layer=l1|l2, result=hit|miss）",
    ("layer", "result"),
)
cache_lock_total = Counter(
    "cache_lock_total",
    "跨进程缓存锁结果（result=acquired|timeout|error）",
    ("result",),
)
# user:snap 读请求合并（singleflight）：role=leader 为真正执行加载的请求，shared 为复用其结果者。
# leader/shared 比值反映合并收益（趋近 1:1 表示热点击穿被有效收敛）。
user_snap_singleflight_total = Counter(
    "user_snap_singleflight_total",
    "user:snap singleflight 请求合并（role=leader|shared）",
    ("role",),
)


counts_reconcile_repeated_total = Counter(
    "counts_reconcile_repeated_total",
    "连续两轮对账都需修正的计数 key 数（对账震荡信号，>0 需人工查写方向）",
)


audit_events_total = Counter(
    "audit_events_total",
    "已消费的审计事件数（action=login_fail|permission_change；§5.2）",
    ("action",),
)


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
graphql_query_depth = Histogram(
    "graphql_query_depth",
    "GraphQL 单次操作的声明深度分布（与 graphql_max_depth 同口径；§2 第 5 条）",
    buckets=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 16),
)


# ---- 跨进程指标中继的自观测（app/core/metrics_relay.py）----
metrics_relay_instances = Gauge(
    "metrics_relay_instances",
    "本轮读到的有效指标快照数（非 API 进程生产者数；0=中继无来源）",
)
metrics_relay_up = Gauge(
    "metrics_relay_up",
    "跨进程指标中继是否在取到数：1=本轮读到 ≥1 份快照，0=无来源/Redis 不可读",
)


# ---- 连接池水位 ----
class DBPoolCollector(Collector):
    """把两个 asyncpg 连接池的水位暴露为 ``db_pool_connections{pool,state}``。"""
    _METRIC = "db_pool_connections"
    _HELP = (
        "数据库连接池水位（pool=web|worker，state=size|checked_out|overflow|checked_in）"
    )

    def describe(self) -> Iterable[GaugeMetricFamily]:
        # 显式给名字：注册期 REGISTRY 会调 describe() 取指标名，若省掉这步它会用
        # collect() 反推——那会在 import/注册时就去碰（可能尚未创建的）引擎。
        yield GaugeMetricFamily(self._METRIC, self._HELP, labels=["pool", "state"])

    def collect(self) -> Iterable[GaugeMetricFamily]:
        from core.db import session as session_mod

        gauge = GaugeMetricFamily(self._METRIC, self._HELP, labels=["pool", "state"])
        for pool_name, engine in (
            ("web", session_mod._async_engine),
            ("worker", session_mod._worker_engine),
        ):
            if engine is None:
                continue  # 惰性池未创建：跳过，绝不在抓取路径上建池
            pool = getattr(engine, "pool", None)
            if pool is None:
                continue  # 测试/替身引擎可能没有 pool，不影响其余池导出
            try:
                states = {
                    # SQLAlchemy QueuePool 语义：size() 是配置的 pool_size（恒为上限），
                    # checked_in() 是空闲连接数，checked_out() 已借出，overflow() 当前溢出量。
                    # checked_out / (size + overflow) 即「当前已开连接里的占用比例」。
                    "size": pool.size(),
                    "checked_out": pool.checkedout(),
                    "overflow": pool.overflow(),
                    "checked_in": pool.checkedin(),
                }
            except Exception:
                logger.exception("读取 %s 连接池水位失败，本池本次不导出", pool_name)
                continue
            for state, value in states.items():
                gauge.add_metric([pool_name, state], float(value))
        yield gauge


# Collector 在进程内只注册一次。
_pool_collector: DBPoolCollector | None = None


def register_pool_metrics_collector() -> DBPoolCollector:
    """幂等注册连接池水位 Collector，返回已注册实例。"""
    global _pool_collector
    if _pool_collector is not None:
        return _pool_collector
    collector = DBPoolCollector()
    try:
        REGISTRY.register(collector)
    except ValueError:
        for existing in REGISTRY._collector_to_names:  # type: ignore[attr-defined]
            if isinstance(existing, DBPoolCollector):
                _pool_collector = existing
                return existing
        raise
    _pool_collector = collector
    return collector


register_pool_metrics_collector()


def setup_metrics(app: FastAPI) -> None:
    """按 settings 装配 /metrics + 自动 HTTP 埋点；关闭或挂载失败均 fail-open（幂等）。"""
    if not settings.metrics_enabled:
        logger.info("Prometheus metrics 已关闭（LKM_METRICS_ENABLED=false）")
        return
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
