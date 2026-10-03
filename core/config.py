import os
import urllib.parse
from typing import ClassVar, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.secrets import reveal

_PERMISSIVE_ENVS: set[str] = {"dev", "local", "test"}

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

    env: str = "dev"

    app_name: str = "LKM-API"
    app_version: str = "0.0.1"
    api_prefix: str = "/api/v1"

    # HTTP 服务进程（backend / auth）的 Host 头白名单，逗号分隔；"*" = 不校验。
    allowed_hosts: str = ""
    # CORS 显式来源白名单，逗号分隔（如 http://localhost:4321,http://localhost:5173）。
    cors_origins: str = ""

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "lkm"
    db_user: str = "postgres"
    db_password: SecretStr = SecretStr("")

    # 连接池（PostgreSQL/asyncpg 生效）
    db_pool_size: int = 10
    db_pool_max_overflow: int = 20
    # 取连接前 ping 探活，剔除坏连接
    db_pool_pre_ping: bool = True
    # 连接达到回收时间后重建。
    db_pool_recycle_s: int = 1800
    db_pool_timeout_s: float = 30.0
    # worker / 后台批处理的**独立**池：
    db_worker_pool_size: int = 5
    db_worker_pool_max_overflow: int = 10

    # JWT 签名：auth 持私钥签发，backend/网关持公钥验签。
    jwt_private_key: SecretStr | None = None
    jwt_public_key: SecretStr | None = None
    # 亦可给 PEM 文件路径（k8s Secret 卷 / compose 只读挂载）；内联值优先于文件。
    jwt_private_key_file: str = ""
    jwt_public_key_file: str = ""
    # 运行期从 AUTH ``/.well-known/jwks.json`` 拉取验签公钥的**刷新间隔**（秒）。
    jwks_refresh_s: int = 300
    # 缓存防穿透的布隆过滤器：
    bloom_filter_enabled: bool = True
    bloom_filter_capacity: int = 100_000
    bloom_filter_error_rate: float = 0.001
    # 白名单「已预热」标记的存活秒数
    bloom_filter_seed_ttl_s: int = 7 * 24 * 3600
    # 帖详情读缓存 TTL。
    content_detail_cache_ttl_s: int = 60
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 后台 cookie 会话：access cookie 存活分钟（refresh 天数复用 refresh_token_expire_days）
    admin_access_cookie_minutes: int = 15
    # 一次性运维脚本与压测入口也统一走 Settings；敏感值不进入 repr。
    admin_password: SecretStr = SecretStr("")
    admin_2fa_dump: str = ""
    bench_user: str = "bench_user"
    bench_password: SecretStr = SecretStr("")
    bench_auth_login_url: str = "http://auth:8001/api/v1/auth/login/password"

    # 登录限流安全参数
    login_ip_max_per_min: int = 20
    login_global_max_per_min: int = 200
    login_window_seconds: int = 60

    # TOTP / 敏感数据加密密钥 — 独立密钥，勿与验证码 pepper 复用
    totp_encryption_key: SecretStr = SecretStr(
        "change-me-totp-encryption-key-at-least-32-bytes"
    )

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
    files_clamav_address: str = ""  # host:port；配置后扫描不可用则拒绝上传
    files_sensitive_terms: str = ""  # 逗号分隔；匹配文件名、描述与可提取正文
    files_preview_max_bytes: int = 50 * 1024 * 1024
    files_backup_dir: str = ""  # 独立挂载卷上的本地备份目录
    files_archive_after_days: int = 365
    files_archive_dir: str = ""  # 独立挂载卷上的低频归档目录
    redis_url: SecretStr = SecretStr(
        ""  # 空串 = 未启用 Redis
    )

    # 第二后端 URL（如 Dragonfly）
    redis_url_secondary: SecretStr = SecretStr("")
    redis_secondary_prefixes: str = ""

    user_snap_l1_enabled: bool = True
    user_snap_l1_ttl_s: float = 10.0
    user_snap_l1_maxsize: int = 10000
    user_snap_singleflight_enabled: bool = True
    # 仅持锁实例回填 L2。
    cache_lock_enabled: bool = True
    cache_lock_ttl_s: float = 5.0
    cache_lock_wait_ms: int = 200

    # timeline/feed 列表校验后使用 msgspec 序列化。
    read_msgspec_enabled: bool = True

    graphql_max_depth: int = 10
    # 查询成本上限：按 schema 真实的字段/列表规模计分
    graphql_max_cost: int = 1000
    # 查询级时间预算（秒）：预算耗尽后拒绝后续 resolver
    graphql_timeout_s: float = 5.0
    # HTTP 兜底超时（秒）
    graphql_hard_timeout_s: float = 10.0
    # GraphQL 端点基址
    graphql_path: str = "/graphql"

    pulsar_url: str = ""
    # Pulsar Admin REST 基址，供 lag 上报拉取订阅 stats。
    pulsar_admin_url: str = ""
    # Admin REST 鉴权令牌（standalone 本地可空；生产设置）。
    pulsar_admin_token: SecretStr = SecretStr("")
    # 租户名：topic 形如 persistent://{tenant}/{namespace}/{name}
    pulsar_tenant: str = "lkm"
    # 消费失败重投上限：超过后进死信 topic persistent://{tenant}/system/dlq
    pulsar_dlq_max_redeliver: int = 1
    # lag 上报周期（秒）；API 进程统计各订阅 msgBacklog 到 Prometheus gauge
    pulsar_lag_interval_s: float = 30.0
    # 调度器运行态心跳周期（秒）：worker-scheduler 进程写 Redis 心跳、
    # API 进程的 reporter 据此 set gauge；心跳 TTL 取本值的 3 倍（无需另配）。
    scheduler_heartbeat_interval_s: float = 10.0
    # readiness 探 broker 健康的 Admin REST 超时（秒）
    pulsar_probe_timeout_s: float = 2.0
    pulsar_probe_cache_s: float = 5.0
    # Pulsar 客户端操作超时（秒）
    pulsar_operation_timeout_s: float = 30.0

    # outbox relay（app/core/outbox_relay.py run_outbox_loop）
    outbox_relay_interval_s: float = 2.0  # relay 轮询周期
    outbox_leader_ttl_s: float = 60.0
    outbox_lock_ttl_s: float = 300.0
    outbox_archive_retention_s: float = 604800.0
    outbox_archive_batch: int = 500
    outbox_archive_interval_s: float = 3600.0
    outbox_scan_window_s: float = 2592000.0

    # ---- 内容域领域事件（content.*，外部检索引擎增量同步的单一数据源）----
    content_events_enabled: bool = True

    # ---- 检索 ----
    # 检索引擎三选一，默认使用 pg。
    search_engine: Literal["pg", "meilisearch", "opensearch"] = "pg"
    # 事件驱动索引同步开关：
    search_sync_enabled: bool = True
    search_sync_batch_size: int = 200
    # Meilisearch（P2）
    search_meili_url: str = ""
    search_meili_api_key: SecretStr = SecretStr("")
    search_meili_index: str = "content"
    # OpenSearch（P3）
    search_opensearch_url: str = ""
    search_opensearch_user: str = ""
    search_opensearch_password: SecretStr = SecretStr("")
    search_opensearch_index: str = "content"

    # ---- 互动计数 ----
    # true（默认）：点赞/收藏/评论与明细同事务原子改计数列，读数为真值。
    # false：回退 Redis 增量 + 每分钟 flush
    counters_write_through: bool = True

    # ---- interaction 域 ----
    # 浏览记录保留期（天）：cron 每天删除超期行。
    interaction_view_log_retention_days: int = 90

    # ---- feed/timeline 物化 ----
    # fanout 写放大封顶：一条内容的受众超过此数即整条跳过写扩散，只把作者记入 Redis 大 V 集合，由读路径实时补拉。
    feed_fanout_max_followers: int = 2000
    # 新关注一位作者时回填其最近 N 条内容进该关注者的物化 feed（0 = 不回填）。
    feed_backfill_limit: int = 50
    # ---- 时间线全量回填 ----
    # 回填按此批量处理；起始时间留空时从最早记录开始。
    feed_backfill_batch_size: int = 500
    feed_backfill_since: str = ""

    # ---- notification 域 ----
    # 同类通知聚合窗口（秒）：同一 (收件人, 类型, 触发者, 目标) 的未读通知在此窗口内合并为一条（payload.count 累加）
    notification_aggregate_window_s: float = 3600.0

    # ---- Prefect 编排 ----
    prefect_enabled: bool = False
    # Prefect API 基址（如 http://prefect-server:4200/api）；prefect_enabled 时必填
    prefect_api_url: str = ""
    # 目标名 "<flow 名>/<deployment 名>"，如 user-dim-reconcile/reconcile
    prefect_deployment: str = ""
    # API 鉴权 token；自托管无鉴权可空
    prefect_api_token: SecretStr = SecretStr("")
    prefect_analytics_deployment: str = ""
    # 运营日报 flow 的目标名
    prefect_ops_daily_deployment: str = ""
    prefect_work_pool: str = "lkm"
    prefect_source: str = "/app"
    prefect_flow_deployment_name: str = "reconcile"
    # 用户维表全量对账的最大拍数
    user_dim_reconcile_max_rounds: int = 200

    # ---- bot 面板 SSO 协议----
    bot_sso_audience: str = "lkm:bot"
    bot_sso_type: str = "bot_sso"
    bot_sso_issuer: str = "lkm-auth"
    bot_sso_account_level: str = "admin"
    bot_sso_ttl_seconds: int = 60

    # ---- ClickHouse 分析管道 ----
    # 默认关：不建连接、导出 no-op、admin 查询端点返回 503。
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

    files_notify_token: SecretStr = SecretStr("")

    # AUTH 读面 HTTP seam：单体单用户快照 miss 回填可跨进程改走 AUTH 读端点。
    auth_http_url: str = ""
    auth_http_token: SecretStr = SecretStr("")
    auth_http_timeout_s: float = 3.0
    auth_http_retries: int = 2
    auth_http_circuit_failures: int = 5
    auth_http_circuit_reset_s: float = 30.0

    # AUTH 独立库
    auth_db_host: str = "localhost"
    auth_db_port: int = 5432
    auth_db_name: str = "lkm_auth"
    auth_db_user: str = "postgres"
    auth_db_password: SecretStr = SecretStr("")
    auth_db_pool_size: int = 10
    auth_db_pool_max_overflow: int = 20
    auth_db_pool_pre_ping: bool = True

    # False 使用 create_all；True 执行 Alembic 迁移。

    use_alembic: bool = False

    sentry_dsn: SecretStr = SecretStr("")
    # Sentry 性能采样率（0~1）；仅 DSN 非空时才生效。
    sentry_traces_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    # metrics 默认启用，可通过 LKM_METRICS_ENABLED=false 关闭。
    metrics_enabled: bool = True
    # /metrics 暴露根路径（不经 api_prefix，供 Prometheus 探抓）
    metrics_endpoint: str = "/metrics"
    # 跨进程指标中继周期（秒）：非 API 进程多久把本进程指标快照写进一次 Redis
    metrics_relay_interval_s: float = 15.0

    # ---- 链路追踪 ----
    # 默认关：dev/test 不埋点、不依赖 collector；生产置 true 且给 OTLP endpoint 才生效。
    otel_enabled: bool = False
    # service.name 覆盖；空则用 app_name（+ auth 进程后缀 -auth）
    otel_service_name: str = ""
    # OTLP/HTTP traces 端点，如 http://otel-collector:4318/v1/traces（指向外部 SigNoz 亦然）
    otel_exporter_otlp_endpoint: str = ""
    # 额外导出头（逗号分隔 k=v，如 SigNoz ingestion key）；空则不带
    otel_exporter_otlp_headers: str = ""
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
    # 寻址风格：S3/MinIO 常用 path-style，OSS/COS 多用 virtual-host。
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    s3_public_addressing_style: Literal["path", "virtual", "auto"] = "path"

    @model_validator(mode="after")
    def _no_insecure_secrets_outside_dev(self) -> "Settings":
        """
        生产（非宽松环境）必须提供真实密钥，禁止用 change-me 占位或空串启动。
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
            ("totp_encryption_key", self.totp_encryption_key),
            ("verification_code_pepper", self.verification_code_pepper),
        ):
            v = reveal(value)
            if _bad(v):
                insecure.append(name)
            elif len(v) < 32:
                insecure.append(f"{name}(too short)")
        pepper = reveal(self.verification_code_pepper)
        if pepper and pepper == reveal(self.totp_encryption_key):
            insecure.append("verification_code_pepper==totp_encryption_key")

        if self.storage_backend == "s3":
            for name, value in (
                ("s3_access_key", self.s3_access_key),
                ("s3_secret_key", self.s3_secret_key),
            ):
                if _bad(reveal(value)):
                    insecure.append(name)
        if self.auth_http_url and _bad(reveal(self.auth_http_token)):
            insecure.append("auth_http_token(missing while auth_http_url set)")
        if self.prefect_enabled and (
            not self.prefect_api_url or not self.prefect_deployment
        ):
            insecure.append(
                "prefect_api_url/prefect_deployment(required while prefect_enabled=true)"
            )
        if self.clickhouse_enabled and not self.clickhouse_url:
            insecure.append("clickhouse_url(required while clickhouse_enabled=true)")
        # RSA 密钥由 readiness 和签发路径分别检查。

        if insecure:
            raise ValueError(
                "Insecure secrets in production (set LKM_ENV to a non-dev value but secrets "
                f"missing/placeholder): {', '.join(insecure)}"
            )
        return self

    @property
    def is_production(self) -> bool:
        """
        生产（非宽松环境）才允许 HttpOnly cookie 走 Secure。
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
        """
        HTTP 服务进程装配期校验：生产必须显式给 Host 白名单。
        刻意**不**放进 ``_no_insecure_secrets_outside_dev``
        校验器：该器按进程执行，而 worker 进程 env 集不同（不承载 HTTP），强校验会误杀。
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
        """
        消息总线是否启用（pulsar_url 非空）。
        发布 fail-open、outbox 入队 gate、relay 空转判定统一引用此属性。
        """
        return bool(self.pulsar_url)

    @property
    def database_url(self) -> str:
        # 密码使用 URL 百分号编码。
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


def is_test_env() -> bool:
    """
    是否处于测试运行：``LKM_ENV=test`` 或 pytest 注入的 ``PYTEST_RUNNING``。
    集中一处，避免各模块各自 ``os.environ.get(...)`` 拼同一判据。
    """
    return settings.env == "test" or bool(os.environ.get("PYTEST_RUNNING"))
