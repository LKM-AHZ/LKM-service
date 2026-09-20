"""全局 pytest fixtures 与配置。

提供隔离数据库异步会话，唯一后端 = PostgreSQL 的 **database-per-test**：session 级建好
「含全部表」的模板库，每个测试用 ``CREATE DATABASE ... TEMPLATE`` 克隆出一个完全私有的库，
测末 DROP。每测试“单长活会话 override”保住了“POST 后再 GET/直查同见未提交数据”的既有语义
（同一连接同一事务），跨测试靠独立库隔离，无残留。

  配方推理：
  - SQLAlchemy Async 不具备 join-external-transaction → 每测一个物理隔离单元是 PG 唯一可靠
    等价；克隆只是把「建隔离单元」的成本从逐表 DDL（~1.26s/测）降到文件拷贝（~85ms/测）。
  - 旧实现为 schema-per-test（每测在同一库上 create_all 全部表再 DROP CASCADE），实测建表
    占单测总耗时七成，故改为模板克隆。细节与限制见下方 database-per-test 段落。

- 主库 biz：``db`` 会话 → 业务模板库（:attr:`settings.database_url` 的 ``Base.metadata``）。
- auth 独立库：``auth_db`` 会话 → auth 模板库（:attr:`settings.auth_database_url` 的
  ``auth_metadata``）。
- 融合库：``fused_db_session`` → 单库含 biz+auth 双 metadata，供 monolith 时代迁移用例。
- client：httpx.AsyncClient + ASGITransport。ASGITransport 默认不触发 app.lifespan，
  避免对真实控制面 init_db() 的副作用；``get_session``/``get_read_session`` 依赖覆盖到
  ``db`` 会话。
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Annotated, Any

# 确保测试始终以 test 标志运行，允许弱 JWT 密钥
os.environ["PYTEST_RUNNING"] = "1"

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck
from hypothesis import settings as _hypothesis_settings
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.core import local_cache as _local_cache
from app.core import singleflight as _singleflight
from app.core.config import settings
from app.db.base import Base, now_iso
from app.db.session import get_read_session, get_session
from app.db.shared_objects import ensure_shared_objects
from app.main import app
from auth.db.base import auth_metadata

# 复用类型的别名，供各测试文件 import 使用
DB = Annotated[AsyncSession, pytest.fixture]
Client = Annotated[AsyncClient, pytest.fixture]


# ───────────────────────────────────────────────────────────────────────
# hypothesis 属性测试 profile（M5 7.2.1）
#
# 属性测试落在同步 @given 里自建 PG schema 引擎（见 tests/prop_pg.py），fixture 为
# 函数作用域、schema 建表慢，故统一放宽 deadline、压低 examples、抑制相关 health check；
# 否则会撞 filterwarnings=["error"] 与默认 200ms deadline。--hypothesis-seed 仍可复现。
# ───────────────────────────────────────────────────────────────────────
_hypothesis_settings.register_profile(
    "lkm",
    deadline=None,
    max_examples=50,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
    ],
)
_hypothesis_settings.load_profile("lkm")


@pytest.fixture(scope="session", autouse=True)
def _ensure_pg_shared_objects() -> None:
    """两个测试库（业务库 + auth 独立库）各自确保装有建表前置的共享对象。

    1. ``pg_trgm`` 扩展（M6.9 搜索 P1 的 trgm 索引 opclass 依赖它）；
    2. ``public.uuid_generate_v7()``（UUID 主键列的 server_default 目标，RFC 9562 uuid7）。

    两者都建在 **public**——而 schema-per-test 的 search_path 不含 public，故模型侧的
    索引 opclass 与列 server_default 都显式限定 ``public.``。auth 是**独立 database**，
    public schema 与业务库互不相通，必须各建一份。幂等；库不可达时静默跳过，交由既有
    DB fixture 给出更明确的连接错误。
    """

    async def _run() -> None:
        # 与生产两条建库链同源（app.db.shared_objects），避免测试与生产漂移。
        from app.db.shared_objects import ensure_shared_objects

        for url in (settings.database_url, settings.auth_database_url):
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    await ensure_shared_objects(conn)
            finally:
                await engine.dispose()

    with contextlib.suppress(Exception):
        asyncio.run(_run())


@pytest.fixture(autouse=True)
def _reset_local_caches() -> Iterator[None]:
    """每测复位 L1 本地缓存与 singleflight flight 表。

    两者均为进程内全局单例，且测试大量复用相同 uid（7/3/42…）；不复位必跨用例串味
    （旧的本地命中掩盖新写、或 flight 表残留）。
    """
    _local_cache.reset()
    _singleflight.reset()
    yield
    _local_cache.reset()
    _singleflight.reset()


# ───────────────────────────────────────────────────────────────────────
# database-per-test：模板库 + 文件级克隆
#
# 旧实现是 schema-per-test：在共享主库上为每个测试 ``create_all`` 全部表、测末 DROP
# CASCADE。实测每测光建表就要 725ms（业务库 61 表）+ 186ms（auth 库 18 表），加 drop 共
# ~1.26s，而单个测试总耗时才 ~1.7s —— 绝大多数测试并不改 schema，这部分纯属重复劳动。
#
# 改为：session 级把「已建好全部表」的库留作模板，每个测试用 PG 原生的
# ``CREATE DATABASE ... TEMPLATE`` 克隆（文件级拷贝，实测 ~55ms），测末 DROP。
# 隔离强度不降反升：每测拿到的是一个**完全私有、零残留**的库，而非共享库里的一个 schema。
#
#   配方推理：
#   - SQLAlchemy Async 不具备 join-external-transaction → 每测一个物理隔离单元仍是 PG 唯一
#     可靠等价（单长活会话内 POST→GET 同见未提交数据），clone 只是把「建隔离单元」的成本
#     从逐表 DDL 降到文件拷贝。
#   - 模板库名含 pid：pytest-xdist 各 worker 是独立进程且进程内测试串行，故各 worker 独占
#     自己的模板库。这既避免争抢，也绕开 PG「模板库被其它会话占用时不可克隆」的限制
#     （实测 8 路并发克隆同一模板库会随机抛 ObjectInUseError）。
#   - CREATE/DROP DATABASE 不能在事务内执行 → 维护连接显式用 AUTOCOMMIT。
#   - 克隆库随模板带来 public schema 里的 uuid_generate_v7()/pg_trgm，无需每测重建。
#   - 融合库（biz+auth 双 metadata）单列一个模板，供 monolith 时代迁移用例克隆。
# ───────────────────────────────────────────────────────────────────────
_PID = os.getpid()
_TMPL_BIZ = f"lkm_tmpl_{_PID}"
_TMPL_AUTH = f"lkm_auth_tmpl_{_PID}"
_TMPL_FUSED = f"lkm_fused_tmpl_{_PID}"

_db_counter = iter(range(10**9))


def _next_db(kind: str) -> str:
    """生成测试专属库名：``<kind><pid>_<n>``。

    pid 保证跨 xdist worker 不撞名（各 worker 独立进程、各自从 0 计数）；kind 区分业务库
    (t)/auth 库 (a)/融合库 (f) —— 三者是同一 PG 实例上的不同 database，库名须全局唯一。
    """
    return f"{kind}{_PID}_{next(_db_counter)}"


def _db_url(base_url: str, db_name: str) -> str:
    """把连接串的库名换成 ``db_name``，host/port/账号/口令原样保留。"""
    return (
        make_url(base_url).set(database=db_name).render_as_string(hide_password=False)
    )


def _maint_engine() -> AsyncEngine:
    """维护连接：连主库、AUTOCOMMIT（建删库不能在事务内）、NullPool（可并发开多条）。"""
    return create_async_engine(
        settings.database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )


async def _build_template(name: str, base_url: str, metadatas: tuple[Any, ...]) -> None:
    """建模板库：template0 起底（不带主库既有数据/扩展）→ 共享对象 → 全量建表。"""
    maint = _maint_engine()
    try:
        async with maint.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    finally:
        await maint.dispose()
    eng = create_async_engine(_db_url(base_url, name), poolclass=StaticPool)
    try:
        async with eng.begin() as conn:
            await ensure_shared_objects(conn)
            for md in metadatas:
                await conn.run_sync(md.create_all)
    finally:
        await eng.dispose()
    await _seal_template(name)


async def _seal_template(name: str) -> None:
    """封库：踢掉模板库上的残留会话，并禁止再连。

    本仓 PG 预载 timescaledb（见 docker-compose postgres 段），其后台 worker
    （``TimescaleDB Worker Scheduler``）会附着到新建的库上。一旦它挂到模板库，
    ``CREATE DATABASE ... TEMPLATE`` 就会随机抛 “source database ... is being accessed
    by other users”（实测长时跑必然出现）。模板建好后不再需要被连接——克隆是文件级拷贝，
    并不要求源库可连（``template0`` 本身就是 ``ALLOW_CONNECTIONS false``）——故封掉根治；
    后续 worker 即便重试也会被拒连，不再占用。
    """
    maint = _maint_engine()
    try:
        async with maint.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await conn.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
    finally:
        await maint.dispose()


async def _drop_database(name: str) -> None:
    """删库（WITH FORCE 连残留会话一并清掉），幂等。"""
    maint = _maint_engine()
    try:
        async with maint.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await maint.dispose()


@pytest.fixture(scope="session", autouse=True)
def _pg_templates() -> Iterator[None]:
    """session 级建三个模板库（业务 / auth / 融合），测毕清理。"""

    async def _build() -> None:
        from app.db.model_registry import ensure_all_models

        ensure_all_models()
        await _build_template(_TMPL_BIZ, settings.database_url, (Base.metadata,))
        await _build_template(_TMPL_AUTH, settings.auth_database_url, (auth_metadata,))
        await _build_template(
            _TMPL_FUSED, settings.database_url, (Base.metadata, auth_metadata)
        )

    async def _cleanup() -> None:
        for name in (_TMPL_BIZ, _TMPL_AUTH, _TMPL_FUSED):
            with contextlib.suppress(Exception):
                await _drop_database(name)

    asyncio.run(_build())
    yield
    asyncio.run(_cleanup())


@contextlib.asynccontextmanager
async def _cloned_session(
    base_url: str, template: str, kind: str
) -> AsyncGenerator[AsyncSession]:
    """克隆模板库 → 交出一个长活会话，退出时删库。

    会话仍是「单长活会话 override」语义（同一连接同一事务，POST→GET 同见未提交数据）。
    删库放在 finally：库名含 pid、建前先 DROP IF EXISTS，故即便上轮崩溃残留也不影响正确性。
    """
    name = _next_db(kind)
    maint = _maint_engine()
    try:
        async with maint.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
        engine: AsyncEngine = create_async_engine(
            _db_url(base_url, name), poolclass=StaticPool
        )
        try:
            # 预热：先建好那唯一物理连接（StaticPool 用毕归还、不关闭）。否则会话首次取连接
            # 才做 provisioning，测试里的并发查询会撞
            # “This session is provisioning a new connection; concurrent operations are not
            # permitted”。旧实现由建表 DDL 天然预热，克隆库必须显式补上。
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
            factory = async_sessionmaker(
                autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
            )
            session: AsyncSession = factory()
            try:
                yield session
            finally:
                await session.close()
        finally:
            await engine.dispose()
    finally:
        with contextlib.suppress(Exception):
            async with maint.connect() as conn:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                )
        await maint.dispose()


# ───────────────────────────────────────────────────────────────────────
# db / auth_db / client
# ───────────────────────────────────────────────────────────────────────
@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession]:
    """主库（biz realm）隔离会话：克隆业务模板库，每测一个独立库 + 长活会话。

    保证“POST 后再测内 GET/直查同见”，测末整库删除隔离。
    """
    async with _cloned_session(settings.database_url, _TMPL_BIZ, "t") as session:
        yield session


@pytest.fixture
async def auth_db() -> AsyncGenerator[AsyncSession]:
    """auth 独立库隔离会话：克隆 auth 模板库（AuthBase 18 表），语义同 :func:`db`。"""
    async with _cloned_session(settings.auth_database_url, _TMPL_AUTH, "a") as session:
        yield session


@pytest.fixture
async def fused_db_session() -> AsyncGenerator[AsyncSession]:
    """S5 拆后迁移期装配：单库内含 biz(Base)+auth(AuthBase) 双 metadata。

    供「monolith 时代集成测试」迁用——它们单会话须同时见 auth(users/profile) 与业务
    (content/blog/files…) 表。两 metadata 覆盖的表名互不相交(已核校验)，可安全同库
    create_all（模板库构建时已建好两套）。测末整库删除。
    """
    async with _cloned_session(settings.database_url, _TMPL_FUSED, "f") as session:
        yield session


@pytest.fixture
async def client(
    db: AsyncSession, auth_db: AsyncSession
) -> AsyncGenerator[AsyncClient]:
    """官方异步 HTTP 客户端：httpx.AsyncClient + ASGITransport。

    不触发 lifespan（避免 init_db 触碰真实控制面/真实数据库），并把 get_session/get_read_session
    覆盖到 ``db`` 会话（使同一测试内 HTTP 请求与测试体的 service 直呼共享同一长活事务，
    得以 flush 未 commit 即 POST→GET 同见）。S5-A2 Step2 起 admin **数据面 reader**端点依赖
    一个 auth 库只读会话（``users_router.get_admin_auth_read_session``），这里同步覆盖到本测
    ``auth_db`` 会话，使 reader 的 user 列表/总数/趋势读到 auth authoritative（在该测试专属
    auth schema）——conftest 不启 seam 时该依赖不被其它端点击活，本覆盖对既有测试零副作用。
    只撤销本测试注入的覆盖键。
    """

    async def override_get_session() -> AsyncGenerator[AsyncSession]:
        yield db

    from app.modules.admin.users_router import (
        get_admin_auth_read_session,
    )

    async def override_auth_read() -> AsyncGenerator[AsyncSession]:
        yield auth_db

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_read_session] = override_get_session
    app.dependency_overrides[get_admin_auth_read_session] = override_auth_read
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_read_session, None)
        app.dependency_overrides.pop(get_admin_auth_read_session, None)


@pytest.fixture
async def auth_front_client(auth_db: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """前台 auth HTTP 宿主（S5 slice-1 收敛样板）：单体 monolith + auth 库会话。

    前台 /auth/* 路由与会话依赖经上述收敛已绑 ``get_auth_session``（auth 独立库）。
    本 fixture 起单体的 ``app.main.app``(ASGITransport, 不触发 lifespan)，并把该通道
    ``get_auth_session`` override 到本测 ``auth_db`` 的 AuthBase 专属 schema —— 使前台
    auth 路由的 User/TOTP/RefreshToken 读写落到该测量 auth 真值，与测试体直呼
    ``auth_db``(flush 未 commit 即 POST→GET 同见) 共享同一长活会话。

    相对既有 ``client``（get_session→业务 db, 服务业务域端点）语义**不变**：本 fixture 专用于
    "前台认证语义" 端点（业务域各自仍走 ``client``），两者 override 键互不污染。
    """
    from auth.db.session import get_auth_session

    async def override_get_auth() -> AsyncGenerator[AsyncSession]:
        yield auth_db

    app.dependency_overrides[get_auth_session] = override_get_auth
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_auth_session, None)


@pytest.fixture
async def auth_app_client(auth_db: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """AUTH 独立进程 admin 会话写面相的 HTTP 客户端（S5-A2 Step0）。

    直接起 :data:`auth.main.app`（module singleton ``create_auth_app``，**不触发
    lifespan**——ASGITransport 默认不跑），并把该 AUTH 进程里唯一 auth-库通道
    ``auth.db.session.get_auth_session`` override 到本测传入的 ``auth_db`` 会话，
    使请求打到 AUTH 进程而 DB 落在 auth 独立库（该测试专属 schema）。

    注意 auth.main 的 ``app`` 与单体的 ``app.main.app`` 是不同实例，各自 dependency_overrides
    互不污染；测毕只撤销本 fixtest 注入的键。auth 写面端点在 auth_router/respond 都走
    ``resp_json``/BizError frame，读取与单体 client 一致（body.code / body.data）。
    """
    from auth.db.session import get_auth_session
    from auth.main import app as auth_app

    async def override_get_auth_session() -> AsyncGenerator[AsyncSession]:
        yield auth_db

    auth_app.dependency_overrides[get_auth_session] = override_get_auth_session
    transport = ASGITransport(app=auth_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        auth_app.dependency_overrides.pop(get_auth_session, None)


# ─────────────────────────────────────────────────────────────────────────────
# auth_user：跨 realm 身份工厂（M3.B S5 拆库后业务测试的生产者）
#
# S5 把 users/profiles 物理迁出 monolith Base.metadata：业务库不再有 users 表，
# 业务行只能引用一个**逻辑 int user_id**（FK→users 已断成裸 int）。本 fixture 让
# 测试先在"该测试专属的 auth 库 schema"（经 auth_db, AuthBase=18 表）写入一个真实
# User(+可选 Profile)，返回其稳定 int id；业务测再把该 id 写进业务表的 int 列。
# 每测 auth schema 独立（Alembic/conftest schema-per-test）→ id 自 1 对齐。
#
# 使用：
#     async def t(auth_db: DB, db: DB):
#         uid = await auth_user_uid(auth_db, username="bob", nickname="Bobby")
#         await db.execute(insert(BizTbl).values(uploader_id=uid, ...))
#
# 对"需展示名/身份存在"的业务读：跨库不许同事务 join → 业务 service 须走
# auth.snapshot 或 auth HTTP seam（auth_http_url/token 启用），不侧挂 auth engine
# 同事务。测试即可用 auth_http 替身 seam（见测试层 HTTP 替身），或直接断言 int 列。
# -----------------------------------------------------------------------------
@dataclass
class AuthUser:
    """在 auth 独立库 schema 建立的用户身份（S5 拆库常驻）。"""

    id: uuid.UUID  # auth 库 uuid 主键：业务行以裸 uuid 列引用此值
    username: str
    account_level: str
    token: str  # 该用户在 auth 库 mint 的 Web Bearer access token（需会话鉴权时代用）


async def auth_user_uid(
    auth_db: AsyncSession,
    *,
    username: str = "alice",
    account_level: str = "normal",
    email: str | None = None,
    nickname: str | None = None,
    avatar: str | None = None,
    role: str = "member",
    with_token: bool = True,
) -> AuthUser:
    """在 auth 独立库 schema 建一线用户并返回 :class:`AuthUser`。

    auth_db 是调用测试内连到 auth 独立 metadata/schema 的会话（Alembic/conftest
    schema-per-test）。用户 id 为 uuid7（PG server_default 生成，本测内稳定）；返回 token
    供把该用户作为 "current 登录身份"发起业务 HTTP（须 seam 支持跨库裁决，或业务 local
    seam 直读）。
    """
    from auth.models import Profile, User
    from auth.security import create_access_token, hashpwd

    user = User(
        username=username,
        email=email,
        hashed_password=await hashpwd("secret123456"),
        account_level=account_level,
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(
        Profile(
            user_id=user.id,
            nickname=nickname,
            avatar=avatar,
            role=role,
        )
    )
    await auth_db.flush()
    token: str | None = None
    if with_token:
        token = create_access_token(
            user_id=user.id,
            account_level=str(user.account_level),
            role=role,
            token_version=user.token_version,
        )
    return AuthUser(
        id=user.id, username=username, account_level=account_level, token=token or ""
    )


@pytest.fixture
async def auth_user_factory(
    auth_db: AsyncSession,
) -> "Any":
    """返回 :func:`auth_user_uid` 绑定到本测 auth schema 的便捷闭包。"""

    async def _make(**kw: Any) -> AuthUser:
        return await auth_user_uid(auth_db, **kw)

    return _make


# ─────────────────────────────────────────────────────────────────────────────
# auth seam realm double（M3.B S5 C）：拆库后业务“display_name/existence”读的跨 realm 落点
#
# 拆库后业务进程不该（也不能）经 `select(User)` 读业务 db 的 users（users 已迁 auth realm）。
# 业务代码的身份/展示读一律走 auth 缝（auth.snapshot + auth HTTP seam：`user_http.enabled()`
# 开启时 deps 鉴权→`authorize_via_seam`、snapshot 单/批读→`fetch_user_http_payload`）。
# 测试要为真正跑业务 HTTP 鉴权 + display-name 可读找一个“指向本测 auth schema 的替身”：
# 本 fixture **开启** seam（配齐 url+token），并把 seam 两个入口 monkeypatch 成按 user_id 直读
# 本测 conftest ``auth_db``（AuthBase 里该测试刚用 auth_user_uid/auth_user_factory 造的真用户）：
#    - ``user_http.authorize_via_seam``：按 auth_db 的 User(+Profile) 裁 is_locked/token_version/
#      account_level/role → verdict（等价 AUTH 进程内 service_authz，不触业务 db）。
#    - ``user_http.fetch_user_http_payload``：按 auth_db 的 User(+Profile) 产出冻结字段 dict +
#      sv（等价 AUTH 读端点 /auth/internal/.../{id}/snapshot，不触业务 db）。
# 于是业务 service 收到的 auth_db 会话无关紧要：seam 已把它们引到 auth realm 真值。每测完毕后
# monkeypatch 自动复原。业务域测试文件把本 fixture 名放进签名即逐个激活（opt-in，勿 autouse——
# auth 域局部 seam OFF 测试须保持 OFF）。
#
# 配合：业务行只写裸 int user_id（= auth_user_uid 返回的 .id）；登录/current 身份请求带
# AuthUser.token。本缝 is ON → 业务 HTTP 鉴权(RBAC/auth)与 display 读都不会落业务 users。
# -----------------------------------------------------------------------------


def _install_user_seam(carrier: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 auth HTTP seam 三个入口替身为按 ``carrier`` 会话/库的 auth 真值直读。

    供 :func:`auth_seam_realm`（carrier=auth_db，独立 auth 库 schema）与
    :func:`auth_seam_fused`（carrier=fused 融合 schema 内的 auth 表）复用，避免两处漂移。
    - authorize_via_seam：按 carrier 的 User(+Profile) 裁 is_locked/…/account_level/role → verdict。
    - fetch_user_http_payload：按 carrier 的 User(+Profile) 产出冻结 dict+sv（等价 AUTH 读端点）。
    - fetch_users_http_batch：单条替身的批量形态（等价 AUTH by-ids 端点，M6.5）。
    - grant_via_seam：升权写替身 → carrier 上 service_authz 原语。
    - mint_bot_sso_ticket：bot 面板 SSO 铸票替身 → 直接调 auth 域签发原语（等价 AUTH 内部端点）。
    """
    from app.core.config import settings as _cfg
    from auth import user_http as uh

    monkeypatch.setattr(_cfg, "auth_http_url", "http://auth-realm-test")
    monkeypatch.setattr(_cfg, "auth_http_token", "internal-test-secret")
    monkeypatch.setattr(_cfg, "auth_http_timeout_s", 1.0)

    async def _authz(*, user_id: uuid.UUID, **_: object) -> dict[str, object]:
        from sqlalchemy import select

        from auth.models import Profile, User

        state: dict[str, object] = {
            "ok": False,
            "cause": None,
            "account_level": None,
            "role": None,
        }
        u = (
            await carrier.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if u is None:
            state["cause"] = "not_found"
            return state
        now = now_iso()
        if u.is_locked and u.locked_until and u.locked_until > now:
            state["cause"] = "locked"
            return state
        prof = (
            await carrier.execute(select(Profile).where(Profile.user_id == user_id))
        ).scalar_one_or_none()
        state["ok"] = True
        state["account_level"] = u.account_level
        state["role"] = prof.role if prof else "member"
        return state

    async def _fetch(user_id: uuid.UUID) -> Any:
        from sqlalchemy import select

        from auth.models import Profile, User
        from auth.snapshot import UserSnapshot, _snap_to_dict

        u = (
            await carrier.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if u is None:
            return None, None
        p = (
            await carrier.execute(select(Profile).where(Profile.user_id == user_id))
        ).scalar_one_or_none()
        snap = UserSnapshot(
            user_id=u.id,
            username=u.username,
            display_name=(p.nickname or u.username) if p else u.username,
            avatar=p.avatar if p else None,
            role=p.role if p else None,
            account_level=str(u.account_level),
            banned=bool(u.is_locked),
            nickname=p.nickname if p else None,
        )
        from app.core.user_cache import version_of_updated_at

        version = version_of_updated_at(u.updated_at) if u.updated_at else None
        return _snap_to_dict(snap), version

    async def _grant(*, kind: str, user_id: uuid.UUID, **kw: object) -> int:
        from auth import service_authz

        if kind == "incubation":
            return await service_authz.grant_incubation(carrier, user_id)
        return await service_authz.grant_exam_unlock(
            carrier,
            user_id,
            unlock_level=kw.get("unlock_level"),
            unlock_role=kw.get("unlock_role"),
        )

    async def _fetch_batch(
        user_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[Any, int | None]]:
        """批量替身（M6.5 by-ids）：逐 id 复用单条替身，缺行回 ``(None, None)``（同端点契约）。"""
        out: dict[uuid.UUID, tuple[Any, int | None]] = {}
        for uid in user_ids:
            out[uid] = await _fetch(uid)
        return out

    async def _mint_bot_ticket(
        *, user_id: uuid.UUID, account_level: str = "admin"
    ) -> dict[str, object]:
        """bot 面板 SSO 铸票替身：直接走 auth 域签发原语（端点侧按 account_level fail-closed）。"""
        from auth.bot_sso import mint_ticket

        ticket, expires_in = mint_ticket(sub=str(user_id), account_level=account_level)
        return {"ticket": ticket, "expires_in": expires_in}

    monkeypatch.setattr(uh, "authorize_via_seam", _authz)
    monkeypatch.setattr(uh, "fetch_user_http_payload", _fetch)
    monkeypatch.setattr(uh, "fetch_users_http_batch", _fetch_batch)
    monkeypatch.setattr(uh, "grant_via_seam", _grant)
    monkeypatch.setattr(uh, "mint_bot_sso_ticket", _mint_bot_ticket)


@pytest.fixture
async def auth_seam_realm(
    auth_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把 auth HTTP seam 在测试内指到本测 auth 独立库 schema（跨 realm 业务读/鉴权替身）。"""
    _install_user_seam(auth_db, monkeypatch)


@pytest.fixture
async def auth_seam_fused(
    fused_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把 auth HTTP seam 在测试内指到本测 fused 融合库里的 auth 表（供 unit+HTTP 装配用例）。

    业务 HTTP(current user/作者 display/升权)走 seam 读 fused schema 里与业务同库的 users，
    无需额外 auth_db/schema；business 路由仍靠 client 的 biz db + auth seam 一同满足。
    """
    _install_user_seam(fused_db_session, monkeypatch)


# ───────────────────────────────────────────────────────────────────────
# 全局 engine 跨 loop 清理
#
# app/db/session.py 与 auth/db/session.py 各有一枚模块级惰性 engine 单例：一旦在
# 某个测试的 event loop 内被 ``new_session()``/后台任务/未 override 的会话路径建立，就被
# 绑定到那个 loop。后续测试用新 loop 复用连接池时会抛 asyncpg
# 「got Future attached to a different loop」。每测后 dispose 两个单例，令各测试都在
# 自身 loop 内重建引擎，消除跨 loop 复用（conftest 自建的 schema 引擎为独立实例，不受影响）。
# ───────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
async def _reset_global_engines() -> AsyncGenerator[None]:
    yield
    from app.db.session import dispose_engine
    from auth.db.session import dispose_auth_engine

    await dispose_engine()
    await dispose_auth_engine()
