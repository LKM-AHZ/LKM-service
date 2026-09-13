# LKM Service

理科迷社区后端服务，基于 FastAPI、SQLAlchemy 和 SQLite/PostgreSQL。

## 当前能力

- 用户系统：本地账号、普通账号、邮箱/手机号注册、密码登录、验证码登录、魔法链接登录。
- Token 体系：JWT access token、refresh token、登出时吊销 refresh token。
- 账号安全：账号等级、锁定状态、登录失败计数、登录限流。
- 2FA：TOTP 设置、验证、恢复码确认、禁用。
- OAuth：GitHub 登录与账号绑定。
- Passkey：WebAuthn 注册、登录、凭据列表和删除。
- 账号恢复：用户恢复、管理员恢复、邮箱/手机号/魔法链接恢复流程。
- 专栏系统：专栏申请、审核、自动创建专栏、专栏文章发布与查询。
- 博客系统：Git 仓库托管博客系列、文章文件读取、星标收藏、评论。
- 数据库：开发环境自动建表；生产环境预期使用 Alembic 管理迁移。

## 项目结构

```text
.
├── main.py                    # 兼容入口：uvicorn main:app
├── app/
│   ├── main.py                # create_app(), lifespan, 异常处理器, 启动安全检查
│   ├── api/router.py          # 统一挂载全部模块 REST 路由
│   ├── ws/                    # WebSocket(broker/manager/router), Redis 订阅推送
│   ├── core/
│   │   ├── config.py          # Settings，读取 LKM_ 前缀环境变量
│   │   ├── err.py             # ErrCode / BizError / ERRTABLE / respond
│   │   ├── apm.py             # Sentry 可观测(DSN 空则跳过)
│   │   ├── redis.py / redis_limiter.py / throttle.py  # Redis 客户端与共享限流
│   │   ├── messaging.py       # 消息总线抽象（routing_key→Pulsar topic、JSON schema、Transport seam）
│   │   └── worker*.py         # Pulsar 订阅入口（send/notify/jobs/user-invalidate/points 三订阅/dlq/outbox）
│   ├── db/
│   │   ├── models.py          # users/profiles/columns 等主模型
│   │   ├── init_db.py         # 开发环境自动建表
│   │   └── session.py         # SQLAlchemy engine/session 依赖
│   └── modules/
│       ├── common.py          # ApiResp, ListData, ModuleStatus
│       ├── auth/              # 注册/登录/token/me、2FA、OAuth、Passkey、恢复、绑定
│       ├── admin/             # 后台(auth/content/data/reports + moderation 见下)
│       ├── articles/          # 官方文章/新闻只读端点
│       ├── blog/              # 博客系列、Git 文件读取、星标、评论、Git HTTP
│       ├── boards/            # 分科板块(已实现:板块/负责人/禁言/准入)
│       ├── columns/           # 专栏申请、专栏、专栏文章
│       ├── exam/              # 考试认证(板块解锁)
│       ├── files/             # 文件库(上传/审核/下载/对象事件 notify)
│       ├── follow/            # 关注(用户/板块)
│       ├── forum/             # 社区帖子/评论/点赞 + GraphQL schema
│       ├── health/            # 健康检查
│       ├── moderation/        # 后台审核(内容/板块)
│       ├── points/            # 积分/成就/排行榜(事件规则引擎)
│       ├── projects/          # 项目广场
│       ├── qa/                # 问答
│       ├── rbac/              # 权限点/RBAC 统一
│       ├── starhope/          # StarHope AI 学习助手
│       ├── storage/           # Local/S3 对象存储抽象
│       └── timeline/          # 时间线
├── alembic/                   # Alembic 环境配置与迁移文件(LKM_USE_ALEMBIC=true 时启用)
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
| Health | `/health` | 健康检查 |
| Auth | `/auth` | 注册（local/normal/phone/email）、登录（password/code/magic-link）、Token 刷新与吊销、用户资料 |
| Auth 2FA | `/auth/2fa` | TOTP 设置、验证、禁用、恢复码确认 |
| Auth OAuth | `/auth/oauth` | GitHub 登录与账号绑定 |
| Auth Passkey | `/auth/passkey` | WebAuthn 注册、登录、凭据管理 |
| Auth Settings | `/auth/settings` | 邮箱/手机号绑定 |
| Auth Recovery | `/auth/recover` | 用户自助恢复 + 管理员恢复流程 |
| Columns | `/columns` | 专栏申请、审核、文章发布 |
| Forum | `/forum` | 社区帖子分页浏览、发布、点赞、删除，评论（含回复与楼层号） |
| Files | `/files` | 文件库上传（pending 待审核）、列表筛选排序、详情浏览计数、下载计数 |
| Blog | `/blog` | 博客系列 CRUD、Git 文件读取、星标、评论 |
| Blog Git | `/blog/git` | Git HTTP 后端（仓库读写，Basic Auth 认证） |
| Boards | `/boards` | 分科板块（已实现：板块组织、负责人流程、禁言与发言准入） |
| Follow | `/users`、`/boards` | 关注用户 / 板块 |
| Timeline | `/timeline` | 关注流 / 时间线（分页 + `X-Total`） |
| Files Notify | `/notify` | 文件对象事件回调（对象存储 Webhook） |
| Articles | `/articles` | 官方文章 / 新闻只读端点 |
| Exam | `/exam` | 考试认证（解锁板块） |
| Projects | `/projects` | 项目广场 CRUD / 审核 |
| QA | `/qa` | 问答提问 / 回答 / 浏览 |
| Points | `/points` | 积分 / 成就 / 排行榜（事件规则引擎） |
| StarHope | `/starhope` | StarHope AI 学习助手 |
| Admin | `/admin` | 后台（登录、用户/内容/举报/文档管理） |
| Moderation | `/admin/moderation` | 后台审核（内容 / 板块） |
| WS | `/ws` | WebSocket（Redis 订阅推送、上传登记等） |

## 身份认证

所有写操作使用 `Authorization: Bearer <access_token>`（JWT），由 `get_current_user` 解析。

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

开发环境启动时会执行：

```python
Base.metadata.create_all(bind=engine)
```

因此 SQLite 本地开发可以自动建表。

生产/有历史数据库的环境需显式设 `LKM_USE_ALEMBIC=true` 走 Alembic 增量迁移；本地从零开发用默认 `false`（`create_all` 自动建表）。（当前 `alembic/versions/` 尚为空，若要在已有库上升级需先补充正式 migration。）

## 运行

```bash
uv sync
uvicorn main:app --reload
```

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
