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
# 消息总线投递失败（publish 抛错 / 不可用），供错误率看板；未配置 broker 属 fail-open
# 不计。由 messaging.publish 唯一计数：outbox relay 经同一 publish 投递，其抛出路径已
# 被此处捕获，relay 不再重复 inc（防同一异常 double-count）。
notify_failed_total = Counter(
    "notify_failed_total",
    "消息总线投递失败次数（publish 抛错 / 不可用，unified at messaging.publish）",
)
# 事件契约违约计数（`app/core/event_contract.py`）：side=produce（messaging.publish 拒发，
# relay 侧另有 permanent_failure_reason 折叠进 event_failures）| consume（worker 丢弃坏消息）。
# fn 取契约登记表里的名字，未登记/非字符串统一归到 "<unknown>"——否则任何脏 payload 都能凭空
# 造出 label 维度（label 无界 = 指标基数爆炸），与 audit_events_total 的白名单同理。
# 两侧都有写出点（consume 侧在 worker 进程），故已进 metrics_relay.RELAYED。
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
# outbox relay 领导者选举事件（蓝图 §5.1 第 7 条：「无 leader 或选主抖动即告警」）。
# event=acquired（本实例当选）/ contended（有他人在跑，正常）/ renew_failed（续约失败，
# 失联/被接管前兆）/ stale_reclaimed（接管了陈旧锁）。renew_failed 持续增长或 acquired
# 频繁交替 = 选主抖动，会让 relay 停摆或抢主风暴——故本指标是那条告警的取数点。
outbox_leader_total = Counter(
    "outbox_leader_total",
    "outbox relay 领导者选举事件（event=acquired|contended|renew_failed|stale_reclaimed）",
    ("event",),
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
# 跨进程缓存锁（B4，蓝图 §5.6 的 L2 double-check）：result=acquired（拿到锁，负责回填）
# / timeout（等锁超时后走无锁直读，fail-open）。timeout 上升说明回填耗时或实例数偏多。
cache_lock_total = Counter(
    "cache_lock_total",
    "跨进程缓存锁结果（result=acquired|timeout）",
    ("result",),
)
# user:snap 读请求合并（singleflight）：role=leader 为真正执行加载的请求，shared 为复用其结果者。
# leader/shared 比值反映合并收益（趋近 1:1 表示热点击穿被有效收敛）。
user_snap_singleflight_total = Counter(
    "user_snap_singleflight_total",
    "user:snap singleflight 请求合并（role=leader|shared）",
    ("role",),
)


# 计数对账震荡信号（蓝图 §5.6「同一批 key 多轮 diff 不降反升即判震荡」）：
# 连续两轮对账都需要修正**同一个** content 计数 key，说明漂移正在被反复制造——典型原因是
# 写方向被破坏（出现双向互写）或计数口径与明细不一致。本计数上升即需人工介入排查，
# 而不是让对账在"改了又漂、漂了又改"里空转。
counts_reconcile_repeated_total = Counter(
    "counts_reconcile_repeated_total",
    "连续两轮对账都需修正的计数 key 数（对账震荡信号，>0 需人工查写方向）",
)


# 审计事件消费计数：审计此前只批量导出到 ClickHouse，属
# 「事后可查」；这里把登录失败/权限变更变成**实时可告警**的流。action 维度取
# ``messaging.RKEY_AUDIT_*`` 的值；告警规则见 deploy/prometheus/rules/lkm-audit.yml。
audit_events_total = Counter(
    "audit_events_total",
    "已消费的审计事件数（action=login_fail|permission_change；§5.2）",
    ("action",),
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
# GraphQL 文档的**声明深度分布**（蓝图 §2 第 5 条「执行时间 / 深度分布 / 被拒查询数」三件套
# 之一，此前只有前两者）。深度值直接取自 ``QueryDepthLimiter`` 的校验回调，故与
# ``LKM_GRAPHQL_MAX_DEPTH`` **同一口径**（strawberry 侧计数：叶字段计 0、非叶字段自 1 起，
# introspection 字段不计）——分布右移即「客户端查询正在变深」的先行信号，无需等被拒才可见。
graphql_query_depth = Histogram(
    "graphql_query_depth",
    "GraphQL 单次操作的声明深度分布（与 graphql_max_depth 同口径；§2 第 5 条）",
    buckets=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 16),
)


# ---- 调度器运行态 ----
scheduler_up = Gauge(
    "scheduler_up",
    "调度器心跳是否新鲜：1=在跑，0=进程没了/卡死/心跳不可读（§5.5 第 6 条，告警取数点）",
)
scheduler_state = Gauge(
    "scheduler_state",
    "调度器运行态：1=运行中，0=已暂停/已停止（心跳上报）",
)
scheduler_jobs = Gauge(
    "scheduler_jobs",
    "调度器已注册的 cron 作业数（待触发队列规模，心跳上报）",
)
scheduler_pending_jobs = Gauge(
    "scheduler_pending_jobs",
    "调度器在途（正在执行）作业数；收尾后保留残余值，供「未停残余」观测",
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
                # 池实现可能不支持这些方法：抓取路径绝不能因单池失败而整体 500
                logger.exception("读取 %s 连接池水位失败，本池本次不导出", pool_name)
                continue
            for state, value in states.items():
                gauge.add_metric([pool_name, state], float(value))
        yield gauge


# 模块级单例：重复调用 register_* 或重复 create_app 都只注册一次
# （prometheus_client 对同名指标/同一 collector 二次注册抛 ValueError）。
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
        # 已在 REGISTRY 里（典型：importlib.reload 把上面的模块级单例重置了）。复用既有
        # 实例而不是抛错，否则 reload 后每个请求都可能因重复注册炸掉；找不到同类型实例
        # 说明是别的命名冲突，原样上抛。
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
