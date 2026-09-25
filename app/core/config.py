import urllib.parse
from typing import ClassVar, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import reveal

# 存在即安全的非生产占位桶：仅当显式认领 dev/local/test 才允许占位密钥。
# 刻意不包含 ""：显式把 LKM_ENV 设成空串即按生产 fail-fast。
# 注意「LKM_ENV 未设置」并不 fail-fast —— env 字段默认值是 "dev"，缺失即按 dev 宽松放行，
# 所以生产部署必须显式设 LKM_ENV=production（漏设的代价是占位密钥被放行）。
_PERMISSIVE_ENVS: set[str] = {"dev", "local", "test"}

# 开发兜底的 CORS 来源白名单（本地前端：社区站 astro/管理台 vite）。
# 生产必须显式配置 LKM_CORS_ORIGINS —— 由 ``assert_web_security_configured()`` 在
# HTTP 服务进程装配期强制（见该方法 docstring 说明为何不放进逐进程校验器）。
_DEV_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:4321",
    "http://localhost:5173",
)


class Settings(BaseSettings):
    # 支持项目根目录的 .env 加载（本地开发）；生产无 .env 时走环境变量/默认值
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="LKM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行环境：字段默认 "dev"，故 LKM_ENV **未设置**时按 dev 宽松放行；只有显式设成 ""
    # 或 production 等非宽松值才收紧（fail-fast 校验见 _no_insecure_secrets_outside_dev）。
    # 生产必须显式设 LKM_ENV=production。
    env: str = "dev"

    app_name: str = "LKM-API"
    app_version: str = "0.0.1"
    api_prefix: str = "/api/v1"

    # ---- 公网安全面（M6.1）----
    # HTTP 服务进程（backend / auth）的 Host 头白名单，逗号分隔；"*" = 不校验。
    # 留空在 dev 等价 "*"；生产由 ``assert_web_security_configured()`` 在应用装配期强制显式给值。
    # 注意须把**内网服务名/回环**一并列入（backend,auth,127.0.0.1,localhost），否则容器
    # healthcheck 直连 127.0.0.1 会被 TrustedHost 判 400 而长期 unhealthy。
    allowed_hosts: str = ""
    # CORS 显式来源白名单，逗号分隔（如 http://localhost:4321,http://localhost:5173）。
    # **仅本地开发有效**：生产不挂应用层 CORSMiddleware，CORS 唯一权威是 APISIX
    # （deploy/apisix/apisix.yaml 的 cors 插件）——生产流量必经网关，两边各挂一份只会
    # 变成需要人工同步的第二真相源。未配则取 _DEV_CORS_ORIGINS 兜底。
    # 装配层保证「"*" 与 allow_credentials 不并存」（见 core/middleware.py）。
    cors_origins: str = ""

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "lkm"
    db_user: str = "postgres"
    db_password: SecretStr = SecretStr("")

    # 连接池（PostgreSQL/asyncpg 生效）
    db_pool_size: int = 10
    db_pool_max_overflow: int = 20
    # 取连接前 ping 探活，剔除坏连接，避免陈旧连接 0 连接时的短暂出错
    db_pool_pre_ping: bool = True

    # JWT 签名密钥 — 所有非测试环境必须覆盖此值
    jwt_secret: SecretStr = SecretStr(
        "change-me-to-a-random-secret-thats-at-least-32-bytes-long"
    )
    jwt_algorithm: str = "HS256"
    # RS256/JWKS（批 5，蓝图 §4.2）：配了 RSA 私钥 PEM 即改用 RS256 签发（``jwt_algorithm``
    # 仅在未配私钥的 HS256 路径生效）。公钥留空则由私钥推导；验签方（主服务/网关）只需公钥。
    jwt_private_key: SecretStr | None = None
    jwt_public_key: SecretStr | None = None
    # 亦可给 PEM 文件路径（k8s Secret 卷 / compose 只读挂载）；内联值优先于文件。
    jwt_private_key_file: str = ""
    jwt_public_key_file: str = ""
    # 双验签灰度：RS256 生效后是否仍接受 HS256 旧 token。存量 token 清空后置 false 关闭。
    # 本仓因批 1 重建库、token 全失效，可直接置 false（偏离蓝图灰度时序，登记 §8）。
    jwt_hs_fallback: bool = True
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 后台 cookie 会话：access cookie 存活分钟（refresh 天数复用 refresh_token_expire_days）
    admin_access_cookie_minutes: int = 15

    # 登录限流安全参数（见 backend_auth_security 记忆）：IP/全局每次数量与窗口秒。
    #
    # **与网关限流的分工，不是重复**（`deploy/apisix/apisix.yaml` 的 auth-login 路由另有
    # 60/60s 的 limit-count，两者语义不同、数值刻意不同，不必同步）：
    #   网关：按 remote_addr 的粗粒度削峰，policy=local（各实例独立计数），把洪水挡在应用前。
    #   此处：**账号级精确锁定** —— 用户名级 + 真实 IP 级（经 `core.client_ip` 读 X-Real-IP）
    #         的 Redis 滑动窗口，跨实例共享；只有这一层才会真正锁账号。
    # 调参请按各自语义调：这里的 IP/全局阈值是"防爆破"，网关那边是"防洪水"。
    login_ip_max_per_min: int = 20
    login_global_max_per_min: int = 200
    login_window_seconds: int = 60

    # TOTP / 敏感数据加密密钥 — 必须与 jwt_secret 分开设置
    totp_encryption_key: SecretStr = SecretStr(
        "change-me-totp-encryption-key-at-least-32-bytes"
    )

    # 验证码 HMAC 盐值 — 必须与 totp_encryption_key 和 jwt_secret 分开设置
    verification_code_pepper: SecretStr = SecretStr(
        "change-me-verification-code-pepper-at-least-32-bytes"
    )

    # OAuth (GitHub)
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/oauth/github/callback"
    frontend_callback: str = "http://localhost:5173/login/success"

    # Passkey (WebAuthn)
    rp_id: str = "localhost"
    rp_name: str = "LKM Service"
    origin: str = "http://localhost:5173"

    blog_repo_dir: str = "blog_repos"
    files_store_dir: str = "files_store"
    max_upload_bytes: int = 100 * 1024 * 1024  # 单文件上传上限 100MB
    redis_url: SecretStr = SecretStr(
        ""  # 空串 = 未启用 Redis；非空走 redis://[user:pass@]host:port[/db]
    )

    # ---- 双级缓存 L1（本地进程内，roadmap §5.6）----
    # user:snap 热读的进程内首级缓存：短 TTL、有界，仅加速不具权威（L2/DB 仍是权威）。
    # 失效经 Redis pub/sub 广播到各实例（见 core/user_cache_events.py）；Redis 未启用时
    # L1 一并关闭（无法跨实例失效，不冒陈旧风险）。
    user_snap_l1_enabled: bool = True
    # L1 短 TTL（秒）：同时是 pub/sub 丢广播时的陈旧窗口上界。
    user_snap_l1_ttl_s: float = 10.0
    # L1 最大条目数（LRU 逐出），防 user 数增长导致进程内存无界。
    user_snap_l1_maxsize: int = 10000
    # 单用户读请求合并（singleflight）：同进程并发 miss 只放一个真去调 AUTH/DB。
    user_snap_singleflight_enabled: bool = True
    # ---- 跨进程缓存锁（B4，蓝图 §5.6 的 L2 double-check）----
    # 多副本下让「仅持锁实例回填 L2」成立（singleflight 只收敛单进程）。全程 fail-open：
    # 等锁超时即无锁直读，不阻塞请求；持锁者崩溃由 TTL 自解。
    cache_lock_enabled: bool = True
    cache_lock_ttl_s: float = 5.0
    cache_lock_wait_ms: int = 200

    # ---- 读热路径序列化（msgspec，roadmap §6.5.2）----
    # timeline/feed 读热列表在 Pydantic 校验后改用 msgspec 出端口（降 CPU）。关闭即回退
    # 既有 Pydantic model_dump + stdlib json 路径（逐字节一致），作回滚开关。
    read_msgspec_enabled: bool = True

    # ---- GraphQL 防护（M6.4）----
    # 默认值由前端现有查询集实测校准（2026-09-17：最大深度 5、最大文档 ≈70 token，取
    # 2×/14× 余量）后写死；前端加查询撞阈值时按需放宽，不随请求动态调整。
    graphql_max_depth: int = 10
    # strawberry 无成本分析器：以「词法 token 数」作文档规模/复杂度上限的代理指标。
    # **0 = 关闭该项**（不注册该限制器）。阈值余量经真机实测：前端最大查询远低于 1000，
    # 连完整的 introspection 文档也只有 163 token（真机 2026-09-17 用 lexer 口径反解），
    # 故默认放行 GraphiQL；置 0 只在需要完全免限时用（生产不建议）。
    graphql_max_tokens: int = 1000
    # 查询级时间预算（秒）：预算耗尽后拒绝后续 resolver，令查询以受控错误收束（不能中断
    # 单个已在 await 中的 resolver，见 app/api/graphql.py 的局限说明）
    graphql_timeout_s: float = 5.0

    # ---- 消息总线（Apache Pulsar，M4 全量迁移）----
    # 空串 = 未启用消息总线（发布 fail-open 返回 False、outbox 不入队、relay 空转）。
    # 非空走 pulsar://host:6650（或 pulsar+ssl://）。
    pulsar_url: str = ""
    # Pulsar Admin REST 基址（如 http://pulsar:8080），供 lag 上报拉取订阅 stats。
    pulsar_admin_url: str = ""
    # Admin REST 鉴权令牌（standalone 本地可空；生产设置）。
    pulsar_admin_token: SecretStr = SecretStr("")
    # 租户名：topic 形如 persistent://{tenant}/{namespace}/{name}
    pulsar_tenant: str = "lkm"
    # 消费失败重投上限：超过后进死信 topic persistent://{tenant}/system/dlq
    pulsar_dlq_max_redeliver: int = 1
    # lag 上报周期（秒）；API 进程统计各订阅 msgBacklog 到 Prometheus gauge
    pulsar_lag_interval_s: float = 30.0
    # readiness 探 broker 健康的 Admin REST 超时（秒）：短超时 fail-fast，防不可达的
    # Pulsar 把就绪探针挂死在连接等待上
    pulsar_probe_timeout_s: float = 2.0
    # 探活「up」结果的缓存秒数：就绪探针可能被高频打，避免每次真打 Admin REST；
    # 只缓存成功（error 不缓存 → 恢复立即可见，也不会把 stale 健康当就绪）
    pulsar_probe_cache_s: float = 5.0
    # Pulsar 客户端操作超时（秒）
    pulsar_operation_timeout_s: float = 30.0

    # outbox relay（app/core/outbox_relay.py run_outbox_loop）
    outbox_relay_interval_s: float = 2.0  # relay 轮询周期（含 follower 重试等待间隔）
    # Redis leader 租约 TTL：多副本部署下同一时刻仅持租约副本 poll；worker 失联后接管
    # 延迟上界≈该 TTL。基值取「远大于单轮 poll 耗时 + 单 tick 周期」，防无故障抢主抖动。
    outbox_leader_ttl_s: float = 60.0
    # 行级认领标记（locked_at/locked_by）的陈旧阈值：超过该时长仍被标记的行视为「持有者
    # 已崩溃」，可被重新领取（防某行被崩溃进程永久占住）。基值须远大于单批投递耗时。
    outbox_lock_ttl_s: float = 300.0
    # 已 published 行的归档保留期与单轮归档批大小（M6.3）：relay 把超过保留期的已投行
    # 先复制到 outbox_archived 冷表再删除，避免 outbox_events 随时间无限增长。
    outbox_archive_retention_s: float = 604800.0  # 7 天
    outbox_archive_batch: int = 500
    # 归档动作的触发间隔（秒）：relay 主循环按此节流执行归档，不另起循环。
    outbox_archive_interval_s: float = 3600.0
    # 领取/归档查询的时间窗下界（秒，批 2）：outbox_events 是 TimescaleDB hypertable
    # （按 created_at 分区），把扫描限定在 created_at >= now-window 可让规划器做 chunk
    # 裁剪、只扫近期分区，避免随表增大而全分区扫描。基值与 Timescale 保留策略阈值对齐
    # （30 天）——窗口外的行会被保留策略 DROP，故不改变可达事件的投递语义；设 <=0
    # 关闭窗口（普通 PG 上无 chunk 收益，且极陈旧滞留行此时可被投递）。
    outbox_scan_window_s: float = 2592000.0

    # ---- 内容域领域事件（content.*，外部检索引擎增量同步的单一数据源）----
    # 关 = 内容落库不入 outbox（回退到「检索引擎无增量来源」的旧态）。只发可见性变化：
    # published/updated/deleted；草稿态不发（未发布内容不对外可见，索引侧无需感知）。
    content_events_enabled: bool = True

    # ---- 检索（M6.9 的 P1 = PG FTS；B2 落地 P2/P3 外部引擎）----
    # 引擎择一：pg（默认，零外部依赖）/ meilisearch（P2）/ opensearch（P3）。
    # 非 pg 时读路径走外部引擎、调用失败 fail-open 回落 PG；写路径经 content.* 事件
    # 增量同步（见 modules/search/sync.py），存量由 reindex flow 全量重建。
    search_engine: Literal["pg", "meilisearch", "opensearch"] = "pg"
    # 事件驱动索引同步开关：关 = 只记账不写索引（外部引擎维护期降噪用）
    search_sync_enabled: bool = True
    search_sync_batch_size: int = 200
    # Meilisearch（P2）；api_key 为 master/search key，无鉴权实例留空
    search_meili_url: str = ""
    search_meili_api_key: SecretStr = SecretStr("")
    search_meili_index: str = "content"
    # OpenSearch（P3）；http_auth 仅在 user 非空时启用
    search_opensearch_url: str = ""
    search_opensearch_user: str = ""
    search_opensearch_password: SecretStr = SecretStr("")
    search_opensearch_index: str = "content"

    # ---- 互动计数（B3）：写穿 vs M6.10 的 Redis write-behind ----
    # true（默认）：点赞/收藏/评论与明细同事务原子改计数列，读数为真值。
    # false：回退 Redis 增量 + 每分钟 flush（保留该路径仅为可回滚）。
    counters_write_through: bool = True

    # ---- interaction 域（M6.6）----
    # 浏览记录保留期（天）：cron 每天删除超期行。view_logs 是高频写表，须有明确上界
    # （行数上界是「用户数 × 内容数」，但历史内容多的站点仍需按时间收敛）。
    interaction_view_log_retention_days: int = 90

    # ---- feed/timeline 物化（M6.11）----
    # fanout 写放大封顶：一条内容的受众（关注作者 ∪ 关注版块）超过此数即整条跳过写扩散，
    # 只把作者记入 Redis 大 V 集合，由读路径实时补拉（既不写放大也不丢内容）。
    feed_fanout_max_followers: int = 2000
    # 新关注一位作者时回填其最近 N 条内容进该关注者的物化 feed（0 = 不回填）。
    feed_backfill_limit: int = 50
    # ---- 时间线全量回填（B5，feed-backfill flow / CLI）----
    # 每源每批处理条数（越大越快、单事务越长）；起始时间留空 = 自最早（全量）。
    feed_backfill_batch_size: int = 500
    feed_backfill_since: str = ""

    # ---- notification 域（M6.8）----
    # 同类通知聚合窗口（秒）：同一 (收件人, 类型, 触发者, 目标) 的未读通知在此窗口内合并为
    # 一条（payload.count 累加），防「一次动作扇出大量通知」的通知风暴。0 = 关闭聚合。
    notification_aggregate_window_s: float = 3600.0

    # ---- Prefect 编排（M5 7.2.5，复杂数据管道 DAG/重试/回填）----
    # 默认关：cron 消费者直调既有函数（现状路径），不依赖 Prefect server，测试/部署零改动。
    # 开启后 handler 经 run_deployment 触发 flow（timeout=0 立即返回，不占 JOB_TIMEOUT）；触发
    # 失败 fail-open 回落直调，保证 crash-safety 对账不漏跑。
    prefect_enabled: bool = False
    # Prefect API 基址（如 http://prefect-server:4200/api）；prefect_enabled 时必填
    prefect_api_url: str = ""
    # 目标名 "<flow 名>/<deployment 名>"，如 user-dim-reconcile/reconcile
    prefect_deployment: str = ""
    # API 鉴权 token；自托管无鉴权可空
    prefect_api_token: SecretStr = SecretStr("")
    # analytics 导出 flow 的目标名，形如 "<flow 名>/<deployment 名>"，如
    # analytics-clickhouse-export/analytics-export；留空则不触发 analytics flow（回落直调导出）。
    prefect_analytics_deployment: str = ""

    # ---- ClickHouse 分析管道（M5 7.2.6，日志/失败事件/审计分析）----
    # 默认关：不建连接、导出 no-op、admin 查询端点返回 503（不返回空数据造成假绿）。
    # 开启需 `--profile clickhouse` 起 clickhouse + vector，并把本组配置下发到
    # backend / jobs worker / prefect-worker。
    clickhouse_enabled: bool = False
    # HTTP 接口基址（如 http://clickhouse:8123；https 走 8443 且 secure）
    clickhouse_url: str = ""
    clickhouse_database: str = "lkm"
    clickhouse_user: str = "default"
    clickhouse_password: SecretStr = SecretStr("")
    # 增量导出的单批窗口（行）：水位之后每次取这么多行，循环至不足一批（命令数恒定）
    clickhouse_export_window: int = 1000
    # admin 查询接口单页上限（用户传入 limit 会被裁剪到此值，防一次拖全表）
    clickhouse_query_limit_max: int = 200

    # MinIO/S3 对象事件回调共享令牌：空串 = 未启用（回调端点一律 401）。
    # 生产必须设置固定随机值，供桶通知 webhook 的 Authorization: Bearer 头校验。
    files_notify_token: SecretStr = SecretStr("")

    # AUTH 读面 HTTP seam（M3 B1.2）：单体单用户快照 miss 回填可跨进程改走 AUTH 读端点。
    #   auth_http_url:   AUTH 进程/服务基址（如 http://auth:8001，不带 api_prefix；本仓库
    #                     内部 client 在运行时拼接 settings.api_prefix）。空串 = 关闭缝
    #                     （默认）→ 快照 miss 继续单体/进程内直读业务 DB（既存 A6 语义零改动）。
    #   auth_http_token: 该内部读端点共享令牌（Bearer）。生产设置固定随机长值；URL 非空而
    #                     token 为空时 seam 不启用（fail-closed，端点一律 401、client fail-open）。
    #   auth_http_timeout_s: 单次 internal read 超时；AUTH 不可达/超时 → client 抛 → seam
    #                     fail-open 回落本进程 DB（读不被某死/慢 AUTH 楔住）。见 B1.2 report。
    auth_http_url: str = ""
    auth_http_token: SecretStr = SecretStr("")
    auth_http_timeout_s: float = 3.0

    # AUTH 独立库：auth 自持数据在专属第二个 PostgreSQL（独立 schema/engine）。
    # auth_* 键与 monolith 的 db_* 正交，统一标准只用 PostgreSQL(asyncpg)。
    auth_db_host: str = "localhost"
    auth_db_port: int = 5432
    auth_db_name: str = "lkm_auth"
    auth_db_user: str = "postgres"
    auth_db_password: SecretStr = SecretStr("")
    auth_db_pool_size: int = 10
    auth_db_pool_max_overflow: int = 20
    auth_db_pool_pre_ping: bool = True

    # schema 初始化策略（开发默认 create_all，免维护增量迁移）：
    #   False（默认）→ init_db 用 Base.metadata.create_all()，只建缺失表、不 ALTER、
    #                  不依赖 Alembic；开发新增表只改 models.py 即可，无需手写迁移文件。
    #             局限：不处理已有表的列变更、不记录 schema 版本（无法增量升级老库）。
    #   True        → 走 Alembic 增量迁移（schema 唯一权威、可回滚、可升级老库）。
    # 建议：生产/有历史数据的环境显式设 LKM_USE_ALEMBIC=true；本地从零开发用默认
    # create_all 免去每张新表写迁移的负担。迁移文件（alembic/versions/*）保留作生产后备。
    use_alembic: bool = False

    # Sentry APM：空串 = 不加载（dev/test 默认关闭，避免拖启动）；配置 DSN 才接入
    sentry_dsn: SecretStr = SecretStr("")
    # Sentry 性能采样率（0~1）；仅 DSN 非空时才生效。
    # 越界值（如误填 50）在装配期就报错：sentry-sdk 对越界采样率只在内部记 error 并使采样
    # 失效，表现为「初始化成功但 tracing 静默全关」，很难排查
    sentry_traces_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    # Prometheus metrics（M0.5.1）：默认开（本地无副作用收集器，成本极低）；
    # 显式 LKM_METRICS_ENABLED=false 可整体关闭（fail-open，不阻塞启动）
    metrics_enabled: bool = True
    # /metrics 暴露根路径（不经 api_prefix，供 Prometheus 探抓）
    metrics_endpoint: str = "/metrics"

    # ---- 链路追踪（OpenTelemetry，M5 7.2.2）----
    # 默认关：dev/test 不埋点、不依赖 collector；生产置 true 且给 OTLP endpoint 才生效。
    # 全程 fail-open：初始化/导出异常只记日志，绝不阻塞启动或请求。
    otel_enabled: bool = False
    # service.name 覆盖；空则用 app_name（+ auth 进程后缀 -auth）
    otel_service_name: str = ""
    # OTLP/HTTP traces 端点，如 http://otel-collector:4318/v1/traces（指向外部 SigNoz 亦然）
    otel_exporter_otlp_endpoint: str = ""
    # 额外导出头（逗号分隔 k=v，如 SigNoz ingestion key）；空则不带
    otel_exporter_otlp_headers: str = ""
    # 单次导出超时（秒）：collector 不可达时据此快速失败，不拖 shutdown
    otel_exporter_timeout_s: float = 2.0
    # 采样率（0~1）；低流量可置 1.0
    otel_sample_ratio: float = 0.1

    # ---- 存储后端 ----
    storage_backend: str = "local"  # local | s3
    s3_endpoint_url: str = ""  # 留空=云 S3 默认 endpoint；填了=MinIO 本地(容器内连接用)
    s3_public_endpoint_url: str = (
        ""  # 直传/下载预签名 URL 对浏览器暴露的公网地址(填该服务的公网 host)
    )
    s3_region: str = ""
    s3_bucket: str = "lkm"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_prefix: str = "files"  # 桶内 key 前缀
    # 寻址风格（蓝图 §6.3）：S3/MinIO 常用 path-style，OSS/COS 多用 virtual-host。
    # 公网 client 单列一项——预签名 URL 的 host 形式由它决定（须与实际请求 host 一致）。
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    s3_public_addressing_style: Literal["path", "virtual", "auto"] = "path"

    @field_validator("jwt_algorithm")
    @classmethod
    def _check_jwt_algorithm(cls, v: str) -> str:
        """jwt_algorithm 取值收敛到 HS256/RS256（拼错或填 none 者装配期即报错）。

        注意不强制「RS256 必须配私钥」：只验签的进程（不调 encode）可以只持公钥，
        硬绑会误杀这类部署；签发侧缺私钥时 jwt_keys.encode 已有明确 RuntimeError。
        """
        if v not in ("HS256", "RS256"):
            raise ValueError(f"LKM_JWT_ALGORITHM 仅支持 HS256/RS256，收到 {v!r}")
        return v

    @model_validator(mode="after")
    def _no_insecure_secrets_outside_dev(self) -> "Settings":
        """生产（非宽松环境）必须提供真实密钥，禁止用 change-me 占位或空串启动。

        宽松环境（dev/local/test/未设）放行，保证本地开发与测试套件不受影响。
        """
        env = (self.env or "").strip().lower()
        if env in _PERMISSIVE_ENVS:
            return self
        placeholders = [
            "change-me",
            "changeme",
            "your-",
            "placeholder",
        ]
        insecure: list[str] = []

        def _bad(v: str) -> bool:
            return not v or any(p in v.lower() for p in placeholders)

        for name, value in (
            ("jwt_secret", self.jwt_secret),
            ("totp_encryption_key", self.totp_encryption_key),
            ("verification_code_pepper", self.verification_code_pepper),
        ):
            v = reveal(value)
            if _bad(v):
                insecure.append(name)
            elif (
                name
                in (
                    "jwt_secret",
                    "totp_encryption_key",
                    "verification_code_pepper",
                )
                and len(v) < 32
            ):
                insecure.append(f"{name}(too short)")
        if reveal(self.jwt_secret) == reveal(self.totp_encryption_key):
            insecure.append("jwt_secret==totp_encryption_key")
        # pepper 同样要求是「独立密钥」（字段注释如此要求）：原先只比 jwt/totp 一对，
        # 漏掉了 pepper 等于另两把之一的情况。空/占位值上面已标记，这里跳过以免重复上报。
        pepper = reveal(self.verification_code_pepper)
        if pepper and pepper in (
            reveal(self.jwt_secret),
            reveal(self.totp_encryption_key),
        ):
            insecure.append("verification_code_pepper==jwt_secret/totp_encryption_key")

        # 注：不在此强制 db_password/redis_url —— 各 worker 进程 env 集不同（如
        # worker-scheduler 不接 DB/Redis），按进程强校验会误杀。仅在「用到了才校验」的
        # 条件字段上补强（storage_backend=s3 的 S3 键、配了 AUTH URL 的 seam token）。
        if self.storage_backend == "s3":
            for name, value in (
                ("s3_access_key", self.s3_access_key),
                ("s3_secret_key", self.s3_secret_key),
            ):
                if _bad(reveal(value)):
                    insecure.append(name)
        # AUTH seam 配齐 URL 即须成对给 token（否则端点一律 401，身份读全线降级）
        if self.auth_http_url and _bad(reveal(self.auth_http_token)):
            insecure.append("auth_http_token(missing while auth_http_url set)")
        # Prefect 编排启用即须给 API 基址与目标 deployment（否则触发必失败）
        if self.prefect_enabled and (
            not self.prefect_api_url or not self.prefect_deployment
        ):
            insecure.append(
                "prefect_api_url/prefect_deployment(required while prefect_enabled=true)"
            )
        # ClickHouse 分析后端启用即须给 HTTP 基址（否则客户端建连必失败）
        if self.clickhouse_enabled and not self.clickhouse_url:
            insecure.append("clickhouse_url(required while clickhouse_enabled=true)")
        # RS256/JWKS：关掉 HS 回退却没有任何 RSA 公钥 → RS 与 HS 两条验签路径都不通，
        # 所有 token 一律被拒。这是**自相矛盾**的配置（与进程 env 集无关），装配期即拦。
        if not self.jwt_hs_fallback and not (
            reveal(self.jwt_public_key).strip()
            or reveal(self.jwt_private_key).strip()
            or self.jwt_public_key_file
            or self.jwt_private_key_file
        ):
            insecure.append(
                "jwt_hs_fallback=false without jwt_public_key/jwt_private_key"
            )

        if insecure:
            raise ValueError(
                "Insecure secrets in production (set LKM_ENV to a non-dev value but secrets "
                f"missing/placeholder): {', '.join(insecure)}"
            )
        return self

    @property
    def is_production(self) -> bool:
        """生产（非宽松环境）才允许 HttpOnly cookie 走 Secure。

        宽松环境（dev/local/test/未设）为 False，cookie 走 http 便于本地联调；
        生产（如 LKM_ENV=production）为 True，要求 https 传输 cookie。
        """
        return (self.env or "").strip().lower() not in _PERMISSIVE_ENVS

    @property
    def allowed_hosts_list(self) -> list[str]:
        """TrustedHost 白名单列表；留空视为 ``["*"]``（不校验，dev 与生产装配期断言兜底）。"""
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()] or ["*"]

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS 来源白名单列表；留空 → dev 本地前端兜底（生产由装配期断言拦截）。"""
        parsed = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return parsed or list(_DEV_CORS_ORIGINS)

    def assert_web_security_configured(self) -> None:
        """HTTP 服务进程装配期校验：生产必须显式给 Host 白名单。

        刻意**不**放进 ``_no_insecure_secrets_outside_dev`` 校验器：该器按进程执行，
        而 worker 进程 env 集不同（不承载 HTTP），强校验会误杀（见路线图 §8 #16 同款
        取舍）。本方法只由 ``app.main.create_app`` / ``auth.main.create_auth_app``
        调用——即真正对外承载请求的进程，缺失即启动失败，不靠"配了才生效"的静默降级。

        ``LKM_CORS_ORIGINS`` **不在**必填项内：生产不挂应用层 CORS，该值在生产不生效，
        唯一权威是 APISIX 的 ``cors`` 插件（见 core/middleware.py 的取舍说明）。

        dev/local/test 直接放行，保持本地零配置可跑。
        """
        if not self.is_production:
            return
        if not self.allowed_hosts.strip():
            raise ValueError(
                "公网安全面未配置（生产必填）：LKM_ALLOWED_HOSTS；"
                "示例 LKM_ALLOWED_HOSTS=lkm-ahz.ltd,www.lkm-ahz.ltd,backend,auth,127.0.0.1"
            )

    @property
    def message_bus_enabled(self) -> bool:
        """消息总线是否启用（pulsar_url 非空）。

        发布 fail-open、outbox 入队 gate、relay 空转判定统一引用此属性。
        """
        return bool(self.pulsar_url)

    @property
    def database_url(self) -> str:
        # 用 quote（空格→%20）而非 quote_plus：userinfo 段不按表单语义解码 '+',
        # 含空格的密码用 quote_plus 会变成字面 '＋'，SQLAlchemy 侧 unquote 后密码就错了
        password = urllib.parse.quote(reveal(self.db_password), safe="")
        return (
            f"postgresql+asyncpg://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def auth_database_url(self) -> str:
        """AUTH 独立库的 PostgreSQL(asyncpg) 连接 URL。"""
        password = urllib.parse.quote(reveal(self.auth_db_password), safe="")
        return (
            f"postgresql+asyncpg://{self.auth_db_user}:{password}"
            f"@{self.auth_db_host}:{self.auth_db_port}/{self.auth_db_name}"
        )


settings = Settings()
