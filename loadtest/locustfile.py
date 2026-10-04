"""LKM 后端热路径负载压测。

覆盖公开只读热路径：**GraphQL 聚合读**（articles / columns / blogSeries / contentItems /
boards / articleCategories）与 REST ``/api/v1/health``；以及一组低频 auth 密码登录
（打 **auth 进程**，受 Redis 限流，用于观测限流下的行为）。

⚠️ **2026-10-01 真机验收修正**：本文件原先按 REST 列表端点（``/api/v1/articles`` 等）编写，
但列表读早已改为 **GraphQL 聚合读**——REST 同名路径现只剩 POST/写操作（``GET`` 要么 404
要么 405）。原脚本在真机上 100% 404/405，等于从未跑通。现改为 GraphQL 查询。

运行（生产/本地起多 worker 后）：
    # 只读热路径（主力；直连 backend）
    uv run locust -f loadtest/locustfile.py LKMReadUser --host http://localhost:8000 \
        --headless -u 50 -r 5 -t 1m --only-summary
    # 登录路径低频观测（须能解析到 auth 进程；绝对 URL 指向 auth）
    uv run locust -f loadtest/locustfile.py LKMAuthUser --host http://localhost:8000 \
        --headless -u 5 -r 1 -t 1m --only-summary

容器内跑（compose 全栈；locust 与 backend 分容器以隔离 CPU）：
    docker run --rm --network lkm-website_lkm --entrypoint locust \
      -v "$PWD/loadtest:/loadtest:ro" lkm-service:latest \
      -f /loadtest/locustfile.py LKMReadUser --host http://backend:8000 \
      --headless -u 50 -r 5 -t 60s --only-summary
"""

from locust import HttpUser, between, task

from core.config import settings
from core.secrets import reveal

# auth 登录探测：密码登录走 Redis 限流，负载下多数会被限流拒(ACCOUNT_LOCKED)，属预期。
# 凭据一律从环境取——明文密码写进仓库等于把 bench 账号密码公开（会被 secret scanner 抓，
# 也可能被真账号复用）；密码默认留空表示「未配置」，此时登录只用来观测 4xx 分支。
_LOGIN_USER = settings.bench_user
_LOGIN_PASSWORD = reveal(settings.bench_password)

# auth 路由必须打 **auth 进程**：backend 无 LKM_AUTH_DB_* 配置，打它上面的 auth 路由会因
# 连不上 lkm_auth 而 500（已知架构事实）。用绝对 URL 覆盖 HttpUser 的 host。
_AUTH_LOGIN_URL = settings.bench_auth_login_url

_GRAPHQL = "/graphql/v1"

# ---- GraphQL 读查询（字段名取自真实 schema；见 2026-10-01 真机 introspect）----
_ARTICLES = (
    "{ articles(pageSize: 20) { total page pages "
    "items { slug title description views likes comments } } }"
)
_ARTICLE_CATEGORIES = "{ articleCategories { slug name articleCount } }"
_COLUMNS = "{ columns(pageSize: 20) { total items { id title } } }"
_BLOG_SERIES = "{ blogSeries(pageSize: 20) { total items { id title } } }"
_CONTENT_ITEMS = "{ contentItems(pageSize: 20) { total items { id title } } }"
_BOARDS = "{ boards { id slug title status isPublic } }"


class LKMReadUser(HttpUser):
    """公开只读热路径：加权打 GraphQL 聚合读 + REST 健康检查。"""

    wait_time = between(0.1, 0.3)

    def _gql(self, name: str, query: str) -> None:
        # GraphQL 恒返 HTTP 200，错误在 body 的 errors 里 —— 只判状态码会把 resolver 报错
        # 也报成通过。故显式检查 errors 并以 catch_response 记失败。
        with self.client.post(
            _GRAPHQL, json={"query": query}, catch_response=True, name=name
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"status {resp.status_code}")
                return
            try:
                if resp.json().get("errors"):
                    resp.failure(f"graphql errors: {resp.json()['errors'][:1]}")
            except Exception:
                resp.failure("non-json body")

    @task(6)
    def articles(self) -> None:
        self._gql("graphql:articles", _ARTICLES)

    @task(4)
    def article_categories(self) -> None:
        self._gql("graphql:articleCategories", _ARTICLE_CATEGORIES)

    @task(4)
    def columns(self) -> None:
        self._gql("graphql:columns", _COLUMNS)

    @task(4)
    def blog_series(self) -> None:
        self._gql("graphql:blogSeries", _BLOG_SERIES)

    @task(4)
    def content_items(self) -> None:
        self._gql("graphql:contentItems", _CONTENT_ITEMS)

    @task(2)
    def boards(self) -> None:
        self._gql("graphql:boards", _BOARDS)

    @task(1)
    def health(self) -> None:
        # 健康检查→聚合 DB+Redis，压测下可观测 DB/Redis 探测路径
        self.client.get("/api/v1/health")


class LKMAuthUser(HttpUser):
    """低频 auth 登录：命中 Redis 限流为预期，用于观测登录路径与限流失效行为。"""

    wait_time = between(1, 3)

    @task
    def login_password(self) -> None:
        # 400/401/403/423(限流) 均为预期分支；只对网络/5xx 计失败
        with self.client.post(
            _AUTH_LOGIN_URL,
            json={"account": _LOGIN_USER, "password": _LOGIN_PASSWORD},
            catch_response=True,
            name="auth:login/password",
        ) as resp:
            if resp.status_code in (200, 400, 401, 403, 423):
                resp.success()
            else:
                # 5xx 与任何「非预期状态」（网关 429、404/405、3xx…）都算失败：
                # 原先 5xx 之外一律 success，等于把登录路径的回归也报成通过。
                resp.failure(f"unexpected status: {resp.status_code}")
