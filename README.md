# LKM Service

理科迷社区后端服务，基于 FastAPI、SQLAlchemy、PostgreSQL，支持 Pulsar / RabbitMQ 消息总线。

> 本文档面向后端开发。完整生产栈由上级目录的 `docker-compose.yml` 或 Kubernetes 清单编排；
> 跨项目入口见 [编排仓库文档地图](../docs/DOCUMENTATION.md)（独立克隆时见 [LKM-Website 文档](https://github.com/LKM-AHZ/LKM-Website/tree/master/docs)）。接口参数和响应模型以运行中的
> `/openapi.json` 为最终依据。

## 环境要求

- Python `>=3.13`
- [uv](https://docs.astral.sh/uv/)（依赖和命令入口）
- PostgreSQL；完整消息、缓存和对象存储功能还需要 所选消息 broker、Redis 或 Dragonfly 与 S3/MinIO

首次安装：

```bash
uv sync
cp .env.example .env
```
请务必把 .env 中标注为 change-me 的部分改为真实值。
`.env` 只用于本地配置，不得提交真实密码、JWT 密钥或内部服务令牌。

## 当前能力

- 认证与账号：本地/普通/邮箱/手机号注册，密码/验证码/魔法链接登录，JWT access+refresh 与登出吊销，账号等级/锁定/失败计数/限流，2FA（TOTP + 恢复码），OAuth（GitHub），Passkey（WebAuthn），账号恢复（自助 + 管理员），邮箱/手机号绑定，Onboarding 引导。
- 认证独立服务（M3/S5 拆库 + 源码层拆包）：`users/profiles` 物理迁出业务库，AUTH 独立 ASGI 进程（`auth/main.py`，顶层包 `auth/` 与 `app/` 同级）独立部署；业务域仅经 `core.ports` / `core.contracts` 和 `user:snap` 缓存读身份，auth 实现由 `boot.assemble` 绑定，边界由 import-linter 跨包契约强制。
- 内容域（content 聚合根）：社区帖子/评论/点赞（同事务维护冗余计数）、分科板块（负责人/禁言/准入）、专栏（申请/审核/文章）、问答、官方文章；只读 GraphQL 聚合。
- 信息流域（feed）：物化时间线与 fanout；关注用户/板块归属 interaction 域。
- 其他业务域：博客（Git 托管/星标/评论/Git HTTP）、文件库（上传/审核/下载）、积分/成就/排行榜（事件规则引擎）、考试认证、项目广场、StarHope AI 学习助手。
- 消息与一致性（M1/M4）：可选 Pulsar / RabbitMQ 的消息总线、事务发件箱（outbox）+ relay、按 `event_id` 幂等消费、死信（`system/dlq`）+ 重投、订阅 lag 上报；points 三订阅扇出隔离。装配时校验订阅与 routing key 的精确 topic 映射；非法 envelope 会进入死信，原始字节以 base64 留存供修正重投。
- 缓存与报表：`user:snap` 双级缓存（L1 进程内有界 TTL/LRU，支持批量读取和失效期间回填校验；L2 Redis 负责版本 CAS + epoch 防复活，singleflight 合并请求，L1 失效经 pub/sub 广播）；`cached_read` 热点缓存按前缀路由，显式失效用短期代次阻止在途旧值回填，跨进程锁关闭时仍正常缓存，锁命令失败立即降级；AUTH 变更事件失效；`user_dim` 离线宽表 ETL 供后台/运营报表（与在线读隔离）。
- 读热序列化（M5 §6.5.2）：timeline、search、files、notification、interaction 列表在 Pydantic v2 校验后经 msgspec 出端口（`LKM_READ_MSGSPEC_ENABLED`，关闭回退旧路径）；基准 `loadtest/bench_read_serialize.py`。
- 可观测：`/metrics`（prometheus-fastapi-instrumentator）+ Sentry（DSN 为空则跳过）。
- 数据库：开发环境自动建表；生产使用 Alembic 迁移（业务库 `alembic/`，auth 库 `alembic_auth/`）。

## 项目结构

目录与入口核对日期：2026-10-31。

```text
.
├── main.py                    # uvicorn main:app → boot.backend:app
├── boot/                      # 组合根：唯一同时装配 app 与 auth 的层
│   ├── assemble.py            # 调用两侧 bootstrap、加载模型/任务、校验端口
│   ├── backend.py             # 先装配，再导入 app.main
│   ├── workers/               # default/send/notify/points*/outbox/dlq 等进程入口
│   └── flows*.py              # Prefect flow 加载与部署入口
├── app/                       # 业务包，只依赖 core，不直接导入 auth/boot
│   ├── main.py                # FastAPI、lifespan、异常处理、GraphQL 上下文
│   ├── bootstrap.py           # 业务模型、任务、错误码、seed 和端口实现登记
│   ├── api/                   # REST/只读 GraphQL 聚合；消费 core 中的额外路由
│   ├── flows/                 # Prefect 业务流程与部分纯体实现
│   ├── ws/                    # WebSocket 路由、连接与 Redis 推送
│   └── modules/
│       ├── registry.py        # 业务 REST/GraphQL 出口与错误码聚合
│       ├── content/           # 帖子/评论；articles/blog/boards/columns/qa 子包
│       ├── interaction/       # 关注、收藏、浏览记录
│       ├── feed/              # 时间线读模型与 fanout
│       ├── admin/             # 管理、审核、报表与事件运维
│       ├── files/             # 文件上传、审核、下载与对象事件
│       ├── notification/      # 站内信、偏好与推送 token
│       ├── search/            # 检索引擎与增量索引
│       ├── points/            # 积分、成就、排行榜
│       ├── treehole/          # 匿名信件、漂流瓶、愿望与私聊
│       │   ├── router.py      # HTTP 参数、会话 Cookie 与响应包装
│       │   ├── deps.py        # 匿名令牌校验、owner 派生与鉴权依赖
│       │   └── services/      # letters/conversations/bottles/wishes/reports 业务操作
│       └── exam/ projects/ starhope/ rbac/ health/
├── auth/                      # 认证与用户档案包，只依赖 core，不导入 app/boot
│   ├── main.py                # 独立 AUTH ASGI 入口
│   ├── bootstrap.py           # 认证路由、模型、任务、错误码与端口实现登记
│   ├── ports_impl.py          # core.ports 的认证侧实现
│   ├── db/                    # AuthBase、独立引擎/会话与 schema 初始化
│   └── router*.py service*.py models.py snapshot.py seams.py …
├── core/                      # 共享基础设施与契约，不依赖 app/auth/boot
│   ├── contracts.py ports/    # 共享类型与跨包调用端口
│   ├── db/                    # 业务库会话、Repository、outbox、模型登记
│   ├── storage/               # Local/S3 对象存储抽象
│   ├── flows/                 # 可由调度回落直调的纯体层
│   └── messaging.py rabbitmq.py cache*.py worker*.py …
├── alembic/                   # 业务库迁移；配置 alembic.ini
├── alembic_auth/              # 认证库迁移；配置 alembic.auth.ini
├── tests/                     # 单元/集成测试；deploy/ 是父仓部署契约
├── scripts/                   # SQL 检查、数据迁移、在线 DDL 等工具
└── loadtest/                  # 压测与序列化基准
```

### 依赖与新增模块

依赖方向为 `boot → app/auth/core`、`app → core`、`auth → core`。业务包通过
`core.ports` 和 `core.contracts` 读取身份与鉴权能力；具体实现在 `auth.ports_impl`，由
`boot.assemble` 绑定。不要在业务模块直接导入 auth 内部实现，也不要把业务模型放进 core。
`uv run lint-imports` 强制四组边界，配置见 `pyproject.toml`。

新增业务域时，在 `app/modules/<domain>/` 内维护路由、schema、service、repository 和模型，
按现有模块需要选择文件；REST/GraphQL 出口在 `__init__.py` 暴露并加入 `registry.MODULES`，
错误码核对 `registry._ERROR_MODULES`；模型和任务分别在 `app.bootstrap` 的
`APP_MODEL_MODULES`、`APP_TASK_MODULES` 登记。子包的错误码须显式纳入聚合。
新增跨包能力先在 `core.ports` 定义契约，再由所属包提供实现并在 bootstrap 登记。

树洞路由只处理 HTTP 边界，业务查询、所有权和可见性校验位于 `treehole/services/`。
共享序列化与限流操作集中在 `services/common.py`；服务只使用调用方传入的会话，不自行提交事务。
该域已纳入 import-linter 的模块间边界，只有匿名举报写入后台审核队列的模型依赖被显式放行。

后端启动链为 `main.py → boot.backend → boot.assemble → app.main`；直接使用
`app.main:app` 会绕过组合根。AUTH 独立服务使用 `auth.main:app`，worker 使用
`python -m boot.workers.<name>`。`core/worker*.py` 承载共享消费逻辑，不是部署装配入口。

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
> 修改接口时同步维护所属模块的 router、schema 和测试；接口清单由运行时 OpenAPI 生成。

下表中的模块前缀均挂载在 `/api/v1` 下。例如表中的 `/auth` 对外完整路径为
`/api/v1/auth`；GraphQL 使用版本端点 `/graphql/v1`。

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
| Admin DLQ | `/admin/dlq` | 死信队列查看 / 修正后入队重放 |
| Admin 发布失败 | `/admin/event-failures` | outbox 发布失败审计 / 修正后入队重放 |
| WS | `/ws` | WebSocket（Redis 订阅推送、上传登记等） |

人工重放：`POST /api/v1/admin/event-failures/{id}/replay` 或 `POST /api/v1/admin/dlq/{id}/requeue`。请求体可省略；修复毒消息时可传 `{"routing_key":"event.apply_point","payload":{"fn":"apply_point_event","args":[...]}}`。重放会与状态标记一同提交到 outbox，原失败记录保留审计；relay 稍后发布，接口成功表示已可靠入队。

## 身份认证

所有写操作使用 `Authorization: Bearer <access_token>`（JWT），由鉴权依赖解析；身份/展示读经 `core.ports.authz` / `core.ports.snapshot`，由 AUTH 实现选择本地或 HTTP 读缝，业务库不直连 `users` 表。

密码使用 Argon2id 哈希；新密码和登录输入最长 1024 个字符，旧短密码仍可登录。JWT 验签强制校验 `aud`、`exp`、`iat`，并限制令牌长度为 8192 字符。前后台刷新令牌每次使用后轮换，但沿用首次签发时的到期时间（默认 7 天，`LKM_REFRESH_TOKEN_EXPIRE_DAYS`），刷新不会无限延长会话；账号锁定时拒绝刷新。access token 通过 `rt_hash` 与对应刷新会话绑定，角色切换和后台 2FA 升阶要求两者匹配。部署更新后，存量 access token 若缺少 `rt_hash`，需先刷新再进行这些操作。管理员 2FA 信任窗口从实际验证时刻起算，刷新不会重置信任起点。

### RBAC0 / RBAC1 / RBAC2 / RBAC3 角色与权限

- AUTH 库的 `profiles.role` 与 `users.account_level` 派生兼容旧账号的基础角色；`user_roles` 保存附加角色。一个用户可有多个角色，角色名格式为 `等级:角色`。
- RBAC1 的角色层级在 `core/rbac_roles.py` 定义：`normal:author → normal:columnist → normal:member` 与 `admin:super_admin → admin:org_member → admin:incubated_member`。高级角色继承低级角色的权限；继承只在同一账号等级内，启动时拒绝未知角色、跨等级边和循环。`role_permissions` 仍是权限映射的实时事实源；禁用直接授权不会被 seed 恢复，但其他激活角色或低级角色的同一权限仍可能授权。
- RBAC2 支持按角色集合和最大数量配置静态职责分离（SSD）与动态职责分离（DSD）。SSD 在角色分配、身份升级及鉴权时检查用户的已授权角色闭包；DSD 在每次鉴权时检查本会话的激活角色闭包。配置分别为 `LKM_RBAC_SSD_CONSTRAINTS`、`LKM_RBAC_DSD_CONSTRAINTS`，JSON 格式如 `[{"roles":["admin:content_reviewer","admin:content_publisher"],"max_roles":1}]`。两项默认 `[]`，因为现有业务没有指定实际互斥职责；配置非法时服务拒绝启动。新增 `admin:content_reviewer` 与 `admin:content_publisher` 细分角色分别授予文章审核、发布权限，可用于实际策略。
- 无冲突时，旧令牌默认激活当前等级下的全部已分配角色。有 DSD 冲突时，默认选择一个符合约束的确定性子集。前台 `POST /api/v1/auth/roles/activate` 传 `{"roles":[...],"refresh_token":"..."}`，返回限定角色的 `access_token`；后台 `POST /api/v1/admin/auth/roles/activate` 传 `{"roles":[...]}` 并更新管理员 cookie。所选角色必须是已分配或继承的授权角色且符合 DSD；选择写入对应刷新会话，刷新轮换和 2FA 升阶保留。空数组可创建不激活任何 RBAC 角色的会话。
- 每枚 access token 代表一个独立的激活会话；切换会签发新令牌，先前令牌仍可用至过期或撤销。DSD 限制的是单个会话中的并发角色；需要同一用户跨会话也不能持有两项职责时应配置 SSD。
- RBAC3 将角色层级与职责分离组合裁决：SSD/DSD 均按继承闭包计数；若高级角色因 DSD 无法激活，默认会话继续尝试其可授权的低级角色，显式激活高级角色仍会被拒绝。对象级代管权限按当前会话的角色权限判定，附加角色取得权限时也能通过服务层的二次属主校验。
- 后台角色管理：`GET /api/v1/admin/auth/users/{user_id}/roles` 查看用户角色；`GET /api/v1/admin/auth/roles/{role_name}/users` 分页查看角色成员；`PUT` / `DELETE /api/v1/admin/auth/users/{user_id}/roles/{role_name}` 分配 / 撤销附加角色。操作要求有效管理员 cookie、1 小时内的 2FA 信任和 `admin:super_admin` 角色；变更会撤销目标用户的旧会话。
- 已压平的业务库与 AUTH 库基线分别包含权限开关、附加角色和会话角色字段。

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
  "message": "OK",
  "data": {},
  "request_id": "..."
}
```

错误响应也使用同一结构，`code` 为业务错误码，`message` 为错误说明，HTTP 状态码由 `ERRTABLE`
映射。`request_id` 与响应头 `X-Request-ID`、结构化日志同源，便于把一次前端报错对到后端日志。

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

- 业务库：开发环境启动执行 `Base.metadata.create_all(bind=engine)` 自动建表（对已存在的表另做 `_sync_additive_schema` **加性补列/补索引**，只增不改）；新库设 `LKM_USE_ALEMBIC=true` 走 `alembic/` 基线与增量迁移。**TimescaleDB**：`outbox_events`/`outbox_archived` 装配为 **hypertable**（按 `created_at` 分区，已投递事件由 relay 归档后在冷表压缩；热表不按时间删除待投递事件），全局 `event_id` 由普通表 `outbox_event_keys` 唯一约束；`points_ledger` 亦转 hypertable 以承载 continuous aggregate `points_daily`；独立 auth 库的 `audit_logs` 也按 `created_at` 分区。扩展不可用时各表保留普通 PG 读写能力。详见父仓库 [部署手册](../docs/DEPLOYMENT.md)。

两条链分别以 `0001_uuid_baseline`、`0001_auth_baseline` 为自包含基线，后续增量保留在各自 `versions/` 目录（个人资料联系方式字段与旧建号脚本写出的无效 `admin:admin` 角色修复已折入 auth 基线）。已应用旧版 revision 的数据库不能直接重跑压平后的基线：部署前须核对并补齐表结构，再将 `alembic_version` 标记为对应基线；新库直接运行两条迁移链的 `upgrade head` 即可。
- AUTH 独立库：模型在 `auth/models.py`，使用 `auth/db/base.py` 的 AuthBase；迁移入口 `alembic_auth/`（`alembic.auth.ini`）。建库脚本位于父仓库的 `deploy/initdb/01-auth-db.sh`。schema 由 **auth 进程启动时**按 `LKM_USE_ALEMBIC` 自持初始化（`auth.db.init.init_auth_db`：非 Alembic 走 `AuthBase.create_all` 并执行加性同步，否则走认证迁移链）——auth 表独立于业务 `Base.metadata`。

大表（`content_items`、`content_comments`、outbox、浏览日志、积分流水）超过 64 MiB
或已转 Timescale hypertable 时，启动过程不会自动对既有表加列/建索引。
先运行 `python -m scripts.online_ddl --help`，按 `add-column` → `backfill` →
`index` → `set-not-null` 分阶段执行；默认仅打印 SQL，显式 `--apply` 才连接业务库。
具体发布顺序及示例见父仓库 [部署手册](../docs/DEPLOYMENT.md)。

## CI 生产部署

GitHub Actions 的 `deploy` 仅在 `master`/`main` 手动触发，在 `production`
environment 中推送带 commit SHA 的镜像，并更新已有集群的 14 个后端
Deployment，等待 rollout 成功。先在父仓库部署 Kubernetes 清单，再于本仓库设置：

- 仓库变量 `REGISTRY_IMAGE`（如 `ghcr.io/lkm-ahz/lkm-service`）；可选
  `REGISTRY_HOST`、`REGISTRY_USER`、`KUBE_NAMESPACE`。
- `production` environment secret `KUBE_CONFIG_B64`：专用 kubeconfig 文件的
  base64 编码，身份需有目标 namespace 的 Deployment `get`、`patch`、`watch` 权限。
- 使用非 GHCR 镜像仓时，另设 `production` environment secret `REGISTRY_TOKEN`。
  私有镜像仓还须在集群预置 imagePullSecret。

`prefect-worker` 每次启动会注册/更新 cron 和 flow deployments，滚动更新会同步调度定义。

## 运行

```bash
uv sync
uv run uvicorn main:app --reload --port 8000
```

独立 AUTH 进程：

```bash
uv run uvicorn auth.main:app --reload --port 8001
```

仅运行 `main:app` 适合常规本地开发。验证拆分部署、跨 AUTH 读缝或生产配置时，必须同时启动
AUTH 进程并正确设置 `LKM_AUTH_HTTP_URL`、`LKM_AUTH_HTTP_TOKEN` 和独立数据库配置。

生产 / 完整栈由父仓库 `docker-compose.yml` 编排 AUTH、业务服务、所选 broker 与各 worker。
Compose 默认 RabbitMQ；直接启动时 Settings 默认 Pulsar，由 `LKM_MESSAGE_BUS` 显式选择。

## 测试

```bash
uv run pytest                   # 默认排除 integration 标记
uv run pytest -m integration    # 需要真实 Redis 等外部依赖
```

安装项目开发依赖后，可运行以下静态检查。CI 中 Ruff、import-linter、裸 SQL 检查和 ty 均为阻断门禁；实际扫描范围见 `.github/workflows/ci.yml`：

```bash
uv run ty check app auth core boot scripts  # 与 CI 扫描范围一致
uv run ruff check     # 代码风格 / 静态检查
uv run ruff format --check .  # 只检查格式
uv run ruff format .          # 写入格式化结果
uv run lint-imports    # 架构依赖边界
uv run python scripts/check_raw_sql.py  # 裸 SQL 边界
```

建议提交前运行：

```bash
uv run pytest
uv run ty check app auth core boot scripts
uv run ruff check
uv run ruff format --check .
uv run lint-imports
uv run python scripts/check_raw_sql.py
```
