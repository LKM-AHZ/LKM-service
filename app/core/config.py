import urllib.parse
from typing import ClassVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 存在即安全的非生产占位桶：仅当显式认领 dev/local/test 才允许占位密钥。
# 刻意不包含 ""/None —— LKM_ENV 缺失（含显式设为空串）一律按生产 fail-fast，
# 避免"忘了设 LKM_ENV=production"时占位密钥悄悄放行。本地开发默认 env="dev" 不受影响。
_PERMISSIVE_ENVS: set[str] = {"dev", "local", "test"}


class Settings(BaseSettings):
    # 支持项目根目录的 .env 加载（本地开发）；生产无 .env 时走环境变量/默认值
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="LKM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行环境：默认 dev（本地），生产显式设 LKM_ENV=production 等非宽松值
    env: str = "dev"

    app_name: str = "LKM-API"
    app_version: str = "0.0.1"
    api_prefix: str = "/api/v1"

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "lkm"
    db_user: str = "postgres"
    db_password: str = ""

    # 连接池（PostgreSQL/asyncpg 生效）
    db_pool_size: int = 10
    db_pool_max_overflow: int = 20
    # 取连接前 ping 探活，剔除坏连接，避免陈旧连接 0 连接时的短暂出错
    db_pool_pre_ping: bool = True

    # JWT 签名密钥 — 所有非测试环境必须覆盖此值
    jwt_secret: str = "change-me-to-a-random-secret-thats-at-least-32-bytes-long"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 后台 cookie 会话：access cookie 存活分钟（refresh 天数复用 refresh_token_expire_days）
    admin_access_cookie_minutes: int = 15

    # 登录限流安全参数（见 backend_auth_security 记忆）：IP/全局每次数量与窗口秒
    login_ip_max_per_min: int = 20
    login_global_max_per_min: int = 200
    login_window_seconds: int = 60

    # TOTP / 敏感数据加密密钥 — 必须与 jwt_secret 分开设置
    totp_encryption_key: str = "change-me-totp-encryption-key-at-least-32-bytes"

    # 验证码 HMAC 盐值 — 必须与 totp_encryption_key 和 jwt_secret 分开设置
    verification_code_pepper: str = (
        "change-me-verification-code-pepper-at-least-32-bytes"
    )

    # OAuth (GitHub)
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/oauth/github/callback"
    frontend_callback: str = "http://localhost:5173/login/success"

    # Passkey (WebAuthn)
    rp_id: str = "localhost"
    rp_name: str = "LKM Service"
    origin: str = "http://localhost:5173"

    blog_repo_dir: str = "blog_repos"
    files_store_dir: str = "files_store"
    max_upload_bytes: int = 100 * 1024 * 1024  # 单文件上传上限 100MB
    redis_url: str = (
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

    # ---- 读热路径序列化（msgspec，roadmap §6.5.2）----
    # timeline/feed 读热列表在 Pydantic 校验后改用 msgspec 出端口（降 CPU）。关闭即回退
    # 既有 Pydantic model_dump + stdlib json 路径（逐字节一致），作回滚开关。
    read_msgspec_enabled: bool = True

    # ---- 消息总线（Apache Pulsar，M4 全量迁移）----
    # 空串 = 未启用消息总线（发布 fail-open 返回 False、outbox 不入队、relay 空转）。
    # 非空走 pulsar://host:6650（或 pulsar+ssl://）。
    pulsar_url: str = ""
    # Pulsar Admin REST 基址（如 http://pulsar:8080），供 lag 上报拉取订阅 stats。
    pulsar_admin_url: str = ""
    # Admin REST 鉴权令牌（standalone 本地可空；生产设置）。
    pulsar_admin_token: str = ""
    # 租户名：topic 形如 persistent://{tenant}/{namespace}/{name}
    pulsar_tenant: str = "lkm"
    # 消费失败重投上限：超过后进死信 topic persistent://{tenant}/system/dlq
    pulsar_dlq_max_redeliver: int = 1
    # lag 上报周期（秒）；API 进程统计各订阅 msgBacklog 到 Prometheus gauge
    pulsar_lag_interval_s: float = 30.0
    # Pulsar 客户端操作超时（秒）
    pulsar_operation_timeout_s: float = 30.0

    # outbox relay（app/core/outbox_relay.py run_outbox_loop）
    outbox_relay_interval_s: float = 2.0  # relay 轮询周期（含 follower 重试等待间隔）
    # Redis leader 租约 TTL：多副本部署下同一时刻仅持租约副本 poll；worker 失联后接管
    # 延迟上界≈该 TTL。基值取「远大于单轮 poll 耗时 + 单 tick 周期」，防无故障抢主抖动。
    outbox_leader_ttl_s: float = 60.0

    # MinIO/S3 对象事件回调共享令牌：空串 = 未启用（回调端点一律 401）。
    # 生产必须设置固定随机值，供桶通知 webhook 的 Authorization: Bearer 头校验。
    files_notify_token: str = ""

    # AUTH 读面 HTTP seam（M3 B1.2）：单体单用户快照 miss 回填可跨进程改走 AUTH 读端点。
    #   auth_http_url:   AUTH 进程/服务基址（如 http://auth:8001，不带 api_prefix；本仓库
    #                     内部 client 在运行时拼接 settings.api_prefix）。空串 = 关闭缝
    #                     （默认）→ 快照 miss 继续单体/进程内直读业务 DB（既存 A6 语义零改动）。
    #   auth_http_token: 该内部读端点共享令牌（Bearer）。生产设置固定随机长值；URL 非空而
    #                     token 为空时 seam 不启用（fail-closed，端点一律 401、client fail-open）。
    #   auth_http_timeout_s: 单次 internal read 超时；AUTH 不可达/超时 → client 抛 → seam
    #                     fail-open 回落本进程 DB（读不被某死/慢 AUTH 楔住）。见 B1.2 report。
    auth_http_url: str = ""
    auth_http_token: str = ""
    auth_http_timeout_s: float = 3.0

    # AUTH 独立库：auth 自持数据在专属第二个 PostgreSQL（独立 schema/engine）。
    # auth_* 键与 monolith 的 db_* 正交，统一标准只用 PostgreSQL(asyncpg)。
    auth_db_host: str = "localhost"
    auth_db_port: int = 5432
    auth_db_name: str = "lkm_auth"
    auth_db_user: str = "postgres"
    auth_db_password: str = ""
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
    sentry_dsn: str = ""
    # Sentry 性能采样率（0~1）；仅 DSN 非空时才生效
    sentry_traces_sample_rate: float = 1.0

    # Prometheus metrics（M0.5.1）：默认开（本地无副作用收集器，成本极低）；
    # 显式 LKM_METRICS_ENABLED=false 可整体关闭（fail-open，不阻塞启动）
    metrics_enabled: bool = True
    # /metrics 暴露根路径（不经 api_prefix，供 Prometheus 探抓）
    metrics_endpoint: str = "/metrics"

    # ---- 存储后端 ----
    storage_backend: str = "local"  # local | s3
    s3_endpoint_url: str = ""  # 留空=云 S3 默认 endpoint；填了=MinIO 本地(容器内连接用)
    s3_public_endpoint_url: str = (
        ""  # 直传/下载预签名 URL 对浏览器暴露的公网地址(填该服务的公网 host)
    )
    s3_region: str = ""
    s3_bucket: str = "lkm"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_prefix: str = "files"  # 桶内 key 前缀

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
        for name, value in (
            ("jwt_secret", self.jwt_secret),
            ("totp_encryption_key", self.totp_encryption_key),
            ("verification_code_pepper", self.verification_code_pepper),
        ):
            v = value or ""
            if not v or any(p in v.lower() for p in placeholders):
                insecure.append(name)
            elif name in ("jwt_secret", "totp_encryption_key") and len(v) < 32:
                insecure.append(f"{name}(too short)")
        if self.jwt_secret == self.totp_encryption_key:
            insecure.append("jwt_secret==totp_encryption_key")
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
    def message_bus_enabled(self) -> bool:
        """消息总线是否启用（pulsar_url 非空）。

        发布 fail-open、outbox 入队 gate、relay 空转判定统一引用此属性。
        """
        return bool(self.pulsar_url)

    @property
    def database_url(self) -> str:
        password = urllib.parse.quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def auth_database_url(self) -> str:
        """AUTH 独立库的 PostgreSQL(asyncpg) 连接 URL。"""
        password = urllib.parse.quote_plus(self.auth_db_password)
        return (
            f"postgresql+asyncpg://{self.auth_db_user}:{password}"
            f"@{self.auth_db_host}:{self.auth_db_port}/{self.auth_db_name}"
        )


settings = Settings()
