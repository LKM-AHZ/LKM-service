# LKM Service

理科迷社区后端服务，基于 FastAPI、SQLAlchemy、PostgreSQL 与 Apache Pulsar。

## 当前能力

- 认证与账号：本地/普通/邮箱/手机号注册，密码/验证码/魔法链接登录，JWT access+refresh 与登出吊销，账号等级/锁定/失败计数/限流，2FA（TOTP + 恢复码），OAuth（GitHub），Passkey（WebAuthn），账号恢复（自助 + 管理员），邮箱/手机号绑定，Onboarding 引导。
- 认证独立服务（M3/S5 拆库）：`users/profiles` 物理迁出业务库，AUTH 独立 ASGI 进程（`app/main_auth.py`）独立部署；业务域仅经 `auth.snapshot` 读缝 + `user:snap` 缓存读身份，边界由 import-linter 强制。
- 内容域（content 聚合根）：社区帖子/评论/点赞（同事务维护冗余计数）、分科板块（负责人/禁言/准入）、专栏（申请/审核/文章）、问答、官方文章；只读 GraphQL 聚合。
- 信息流域（feed）：关注用户/板块 + 时间线（分页 + `X-Total`）。
- 其他业务域：博客（Git 托管/星标/评论/Git HTTP）、文件库（上传/审核/下载）、积分/成就/排行榜（事件规则引擎）、考试认证、项目广场、StarHope AI 学习助手。
- 消息与一致性（M1/M4）：Pulsar 全站消息总线、事务发件箱（outbox）+ relay、按 `event_id` 幂等消费、死信（`system/dlq`）+ 重投、订阅 lag 上报；points 三订阅扇出隔离。
- 缓存与报表：`user:snap` cache-through（版本 CAS + 失效 epoch 防复活）、AUTH 变更事件失效；`user_dim` 离线宽表 ETL 供后台/运营报表（与在线读隔离）。
- 可观测：`/metrics`（prometheus-fastapi-instrumentator）+ Sentry（DSN 为空则跳过）。
- 数据库：开发环境自动建表；生产使用 Alembic 迁移（业务库 `alembic/`，auth 库 `alembic_auth/`）。

## 项目结构

```text
.
├── main.py                    # 兼容入口：uvicorn main:app
├── app/
│   ├── main.py                # create_app()：单体(业务域 + 前台 auth 面), lifespan/异常处理/启动安全检查
│   ├── main_auth.py           # AUTH 独立 ASGI 进程入口(auth-only, compose 服务 auth)
│   ├── health_auth.py         # AUTH 进程专属 liveness/readiness
│   ├── api/
│   │   ├── router.py          # 由 registry.MODULES 驱动挂载全部模块 REST 路由
│   │   └── graphql.py         # GraphQL 装配(只读聚合)
│   ├── ws/                    # WebSocket(broker/manager/router), Redis 订阅推送
│   ├── core/                  # 确定性共享层(不得依赖 modules)
│   │   ├── config.py / err.py / common.py / logging.py / apm.py
│   │   ├── cache.py / user_cache.py                        # L2 缓存 / user:snap(版本 CAS + epoch)
│   │   ├── messaging.py / outbox_relay.py / pulsar_lag.py  # 总线抽象 / 发件箱 relay / lag 指标
│   │   ├── jobs.py / task_registry.py / scheduler.py       # 任务注册与调度
│   │   ├── worker*.py         # Pulsar 订阅进程(send/notify/jobs/points 三订阅/dlq/outbox/scheduler/default)
│   │   └── redis.py / redis_limiter.py / throttle.py       # Redis 客户端与共享限流
│   ├── db/                    # 基础设施层(不得反向依赖 modules)
│   │   ├── base.py / session.py / repo.py / model_registry.py
│   │   ├── auth_base.py / auth_session.py  # auth 独立库 metadata / 会话
│   │   ├── models.py / outbox.py / event_processed.py / event_failure.py / user_dim.py
│   │   └── init_db.py         # 开发环境自动建表
│   └── modules/
│       ├── registry.py        # 模块注册表(路由/模型/任务统一出口)
│       ├── auth/              # 认证自有域:登录/2FA/OAuth/Passkey/恢复/authz/onboarding
│       │                      #   + snapshot 读缝、user_http seam、events 失效、user_dim_sync ETL
│       ├── content/           # 内容聚合根:models/router/service/graphql + boards/columns/qa 子包
│       ├── feed/              # 信息流域:关注 + 时间线 + GraphQL
│       ├── admin/             # 后台(users/content/reports/auth/dlq + moderation + dim_report)
│       ├── blog/              # 博客系列、Git 文件读取、星标、评论、Git HTTP
│       ├── files/             # 文件库(上传/审核/下载/对象事件 notify)
│       ├── points/            # 积分/成就/排行榜(事件规则引擎)
│       ├── projects/ exam/ articles/ starhope/
│       └── rbac/ storage/ health/   # 权限点/对象存储抽象/健康检查
├── alembic/                   # 业务库 Alembic 环境与迁移(LKM_USE_ALEMBIC=true 时启用)
├── alembic_auth/              # auth 独立库 Alembic 环境与迁移
├── tests/
├── pyproject.toml
└── uv.lock
```

## 接口概览

基础入口：

```text
GET  /                              # 根路径探活，不走 ApiResp
GET  /api/v1/health                 # 健康检查
GET  /api/v1/boards/status          # 分科板块模块状态
```

> 完整、实时的接口文档由后端启动时自动生成，**以运行时为准**（无需手动维护）：
> - **ReDoc（推荐阅读）**：`http://localhost:8000/redoc`
> - **Swagger UI（交互调试）**：`http://localhost:8000/docs`
> - **原始 OpenAPI JSON**：`http://localhost:8000/openapi.json`
>
> `docs/openapi/` 下那份手写 YAML 已过时（止于 2026-08，未含后续新增的 timeline/follow/points 等域），仅作历史参考。

以下为接口分组摘要：

| 模块 | 前缀 | 说明 |
|------|------|------|
| Health | `/health` | 健康检查（复合 DB/Redis/AUTH） |
| Auth | `/auth` | 注册（local/normal/phone/email）、登录（password/code/magic-link）、Token 刷新与吊销、用户资料 |
| Auth 2FA | `/auth/2fa` | TOTP 设置、验证、禁用、恢复码确认 |
| Auth OAuth | `/auth/oauth` | GitHub 登录与账号绑定 |
| Auth Passkey | `/auth/passkey` | WebAuthn 注册、登录、凭据管理 |
| Auth Settings | `/auth/settings` | 邮箱/手机号绑定 |
| Auth Recovery | `/auth/recover` | 用户自助恢复 + 管理员恢复流程 |
| Auth Onboarding | `/auth/onboarding` | 新人引导流程 |
| Auth Internal | `/auth/internal` | AUTH 进程内部读缝（业务进程跨进程读身份 / user:snap 回填） |
| Content | `/content` | 内容聚合：社区帖子/评论/点赞（同事务冗余计数） |
| Boards | `/content/boards`、`/boards` | 分科板块（板块组织、负责人流程、禁言与发言准入） |
| Columns | `/columns` | 专栏申请、审核、文章发布 |
| QA | `/qa` | 问答提问 / 回答 / 浏览 |
| Articles | `/articles` | 官方文章 / 新闻只读端点 |
| Timeline | `/timeline` | 关注流 / 时间线（分页 + `X-Total`） |
| Follow | `/users` | 关注用户（板块关注见 Boards） |
| Files | `/files` | 文件库上传（pending 待审核）、列表筛选排序、详情浏览计数、下载计数 |
| Blog | `/blog` | 博客系列 CRUD、Git 文件读取、星标、评论 |
| Blog Git | `/blog/git` | Git HTTP 后端（仓库读写，Basic Auth 认证） |
| Files Notify | `/notify` | 文件对象事件回调（对象存储 Webhook） |
| Exam | `/exam` | 考试认证（解锁板块） |
| Projects | `/projects` | 项目广场 CRUD / 审核 |
| Points | `/points` | 积分 / 成就 / 排行榜（事件规则引擎） |
| StarHope | `/starhope` | StarHope AI 学习助手 |
| Admin | `/admin` | 后台（登录、用户管理） |
| Admin Auth | `/admin/auth` | 后台认证管理 |
| Admin Content | `/admin/content` | 后台内容管理 |
| Admin Moderation | `/admin/moderation` | 后台审核（内容 / 板块） |
| Admin DLQ | `/admin/dlq` | 死信队列查看 / 重投 |
| WS | `/ws` | WebSocket（Redis 订阅推送、上传登记等） |

## 身份认证

所有写操作使用 `Authorization: Bearer <access_token>`（JWT），由鉴权依赖解析；身份/展示读经 AUTH 读缝（`app/modules/auth/snapshot.py` / `user_http.py`），业务库不直连 `users` 表。

> **部署要求**：S5 拆库后 `users/profiles` 只在 auth 独立库，业务库已无 `users` 表。生产必须同时配置
> `LKM_AUTH_HTTP_URL`（compose 默认 `http://auth:8001`）与 `LKM_AUTH_HTTP_TOKEN`（backend 与 auth 两侧同值）；
> 缺任一项 seam 关闭，`snapshot` 会回落业务库直查已迁出的 `users` 表而 `UndefinedTable`，身份展示读、
> 考试解锁/项目纳入等升权写将全线失败（本地默认 `LKM_ENV` 非 production 时 url+token 未配同样会关闭 seam）。
>
> 另外 **jobs worker**（`worker` 服务，跑 `auth.tasks` 的 user_dim 离线 ETL）需配 `LKM_AUTH_DB_*` 直连
> auth 库读源（User/Profile），再写业务库 `user_dim`——ETL 为跨 realm 双会话
> （`user_dim_sync` 入口接收 `(source_db, target_db)`），不可单会话跨库 join。

Git HTTP 端点（`/blog/git`）使用 HTTP Basic Auth（用户名+密码）。

## 响应结构

普通业务接口统一返回 `ApiResp`：

```json
{
  "code": 0,
  "msg": "OK",
  "data": {}
}
```

错误响应也使用同一结构，`code` 为业务错误码，`msg` 为错误说明，HTTP 状态码由 `ERRTABLE` 映射。

模块状态接口，如 `/api/v1/boards/status`、`/api/v1/columns/status`，直接返回 `ModuleStatus`，包含：

```json
{
  "module": "columns",
  "status": "implemented_minimal",
  "responsibility": "...",
  "next_steps": []
}
```

根路径 `/` 不走统一响应结构，仅返回：

```json
{"message": "OK"}
```

## 数据库与迁移

- 业务库：开发环境启动执行 `Base.metadata.create_all(bind=engine)` 自动建表；生产/已有历史库设 `LKM_USE_ALEMBIC=true` 走 `alembic/` 增量迁移（现有 12 个版本，含 outbox、event_processed/event_failure、user_dim 等）。
- AUTH 独立库：表定义在 `app/db/auth_base.py`（AuthBase，18 张），迁移入口 `alembic_auth/`（`alembic.auth.ini`，含基线 `a0b1c2d3e4f5`）；库初始化脚本 `deploy/initdb/01-auth-db.sh`。schema 由 **auth 进程启动时**按 `LKM_USE_ALEMBIC` 自持初始化（`init_auth_db`：非 alembic 走 `AuthBase.create_all`，否则走第二迁移链）——auth 表已迁出单体 `Base.metadata`，业务进程不再建它们。

## 运行

```bash
uv sync
uvicorn main:app --reload
```

生产 / 完整栈（含 Pulsar、AUTH 独立服务、各 worker）用仓库根目录 `docker-compose.yml` 编排启动。

## 测试

```bash
uv run pytest -v
```

如果安装了项目开发依赖，可以继续运行静态检查（当前门禁：**ty 0 诊断 + ruff 干净**；`basedpyright` 已降级为可选）：

```bash
uv run ty check       # 硬门禁：类型检查 0 诊断
uv run ruff check     # 代码风格 / 静态检查
uv run ruff format    # 代码格式化
```
