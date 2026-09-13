"""全局 pytest fixtures 与配置。

提供隔离数据库异步会话，唯一后端 = PostgreSQL 的 **schema-per-test**：为每个测试独占一个
PostgreSQL schema，并把该测试唯一引擎所有连接的 search_path 指到它，``create_all`` 落在该
schema，测末 drop cascade。每测试“单长活会话 override”保住了“POST 后再 GET/直查同见未提交
数据”的既有语义（同一连接同一事务），跨测试靠 schema 隔离，无残留。

  配方推理：
  - SQLAlchemy Async 不具备 join-external-transaction → schema-per-test 是 PG 唯一可靠等价。
  - search_path 经连接级 URL options ``-csearch_path=<schema>`` 强制，比逐会话 SET 可靠
    （连接池回取的每根连接都带同一 schema）。
  - 建 schema 必须先于指向它的连接（否则 search_path 连不存在的 schema 会失败）。

- 主库 biz：``db`` 会话 → 用 :attr:`settings.database_url` 的 ``Base.metadata``。
- auth 独立库：``auth_db`` 会话 → 用 :attr:`settings.auth_database_url` 的
  ``auth_metadata``（auth 表建独立库测试 schema）。
- client：httpx.AsyncClient + ASGITransport。ASGITransport 默认不触发 app.lifespan，
  避免对真实控制面 init_db() 的副作用；``get_session``/``get_read_session`` 依赖覆盖到
  ``db`` 会话。
"""

import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated, Any

# 确保测试始终以 test 标志运行，允许弱 JWT 密钥
os.environ["PYTEST_RUNNING"] = "1"

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import settings
from app.db.auth_base import auth_metadata
from app.db.base import Base, now_iso
from app.db.session import get_read_session, get_session
from app.main import app

# 复用类型的别名，供各测试文件 import 使用
DB = Annotated[AsyncSession, pytest.fixture]
Client = Annotated[AsyncClient, pytest.fixture]


# ───────────────────────────────────────────────────────────────────────
# schema-per-test 工具（PG 分支用）：建/命名/drop 一个测试专属 schema
# ───────────────────────────────────────────────────────────────────────
_schema_counter = iter(range(10**9))


async def _pg_schema_engine(
    url: str, schema: str, *, metadata: Any
) -> AsyncEngine:
    """建立指向测试专属 PG schema 的 StaticPool 单连接引擎，并已建好 DDL。

    asyncpg 不接受 URL query ``options``；改用 StaticPool 保持单物理连接，在引擎那唯一
    连接的会话上 ``SET search_path``（对连接级永久生效，无需逐回连重设），随后的
    ``create_all`` 与所有测试会话都落在这个 schema（整测试共享同一连接、同见未 commit
    数据），跨测试靠 schema 隔离。
    """
    eng: AsyncEngine = create_async_engine(url, poolclass=StaticPool)
    async with eng.begin() as conn:
        # DROP IF EXISTS + CREATE：保证残留/重复 run 也干净（schema 名 t<n> 独立不冲突）。
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "t{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "t{schema}"'))
        await conn.execute(text(f'SET search_path TO "t{schema}"'))
        await conn.run_sync(metadata.create_all)
    return eng


async def _drop_schema(url: str, schema: str) -> None:
    """用完清理测试 schema（隔离到别的测试不泄漏）。"""
    eng = create_async_engine(url, poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "t{schema}" CASCADE'))
            await conn.commit()
    finally:
        await eng.dispose()


# ───────────────────────────────────────────────────────────────────────
# db / auth_db / client
# ───────────────────────────────────────────────────────────────────────
async def _pg_realm_session(
    url: str, metadata: Any
) -> tuple[AsyncEngine, AsyncSession, str]:
    """建一个指向独立测试 PG schema 的引擎与会话，返回 (engine, session, schema)。

    ``schema`` 为 :func:`_pg_schema_engine` 建 schema 的计数串，测毕可直接交给
    :func:`_drop_schema` 清场。
    """
    schema = str(next(_schema_counter))
    engine: AsyncEngine = await _pg_schema_engine(url, schema, metadata=metadata)
    session_factory = async_sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )
    session: AsyncSession = session_factory()
    return engine, session, schema


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession]:
    """主库（biz realm）隔离会话。

    经 :func:`settings.database_url` 的真实 PostgreSQL 上建独立测试 schema（create_all 在其上），
    每测一个长活会话 override，保证“POST 后再测内 GET/直查同见”；测末 drop schema 隔离。
    """
    url = settings.database_url
    engine, session, schema = await _pg_realm_session(url, Base.metadata)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()
        await _drop_schema(url, schema)


@pytest.fixture
async def auth_db() -> AsyncGenerator[AsyncSession]:
    """auth 独立库隔离会话。

    auth 真实 PG（auth_database_url）上独立测试 schema，auth_metadata（AuthBase）create_all
    落于此。会话/factory 语义与 :func:`db` 一致。
    """
    url = settings.auth_database_url
    engine, session, schema = await _pg_realm_session(url, auth_metadata)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()
        await _drop_schema(url, schema)


@pytest.fixture
async def fused_db_session() -> AsyncGenerator[AsyncSession]:
    """S5 拆后迁移期装配：单 PG schema 内含 biz(Base)+auth(AuthBase) 双 metadata。

    供「monolith 时代集成测试」迁用——它们单会话须同时见 auth(users/profile) 与业务
    (content/blog/files…) 表。两 metadata 覆盖的表名互不相交(已核校验)，可安全同 schema
    create_all。用 asyncpg ``server_settings.search_path`` 让本引擎每连接都落该 schema，
    多会话/跨连接也稳定(纯 BEGIN 内 SET 会被 rollback 丢弃)。测毕 drop schema。
    """

    def _ensure() -> None:
        from app.db.model_registry import ensure_all_models

        ensure_all_models()

    _ensure()
    url = settings.database_url
    schema = str(next(_schema_counter))
    boot: AsyncEngine = create_async_engine(url)
    try:
        async with boot.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "f{schema}" CASCADE'))
            await conn.execute(text(f'CREATE SCHEMA "f{schema}"'))
    finally:
        await boot.dispose()
    engine: AsyncEngine = create_async_engine(
        url,
        poolclass=StaticPool,
        connect_args={"server_settings": {"search_path": f"f{schema}"}},
    )
    from app.db.auth_base import auth_metadata
    from app.db.base import Base

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(auth_metadata.create_all)
        session_factory = async_sessionmaker(
            autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
        )
        session: AsyncSession = session_factory()
        try:
            yield session
        finally:
            await session.close()
    finally:
        await engine.dispose()
        _drop_conn = create_async_engine(url, poolclass=NullPool)
        try:
            async with _drop_conn.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "f{schema}" CASCADE'))
        finally:
            await _drop_conn.dispose()


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
    from app.db.auth_session import get_auth_session

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

    直接起 :data:`app.main_auth.app`（module singleton ``create_auth_app``，**不触发
    lifespan**——ASGITransport 默认不跑），并把该 AUTH 进程里唯一 auth-库通道
    ``app.db.auth_session.get_auth_session`` override 到本测传入的 ``auth_db`` 会话，
    使请求打到 AUTH 进程而 DB 落在 auth 独立库（该测试专属 schema）。

    注意 main_auth 的 ``app`` 与单体的 ``app.main.app`` 是不同实例，各自 dependency_overrides
    互不污染；测毕只撤销本 fixtest 注入的键。auth 写面端点在 auth_router/respond 都走
    ``resp_json``/BizError frame，读取与单体 client 一致（body.code / body.data）。
    """
    from app.db.auth_session import get_auth_session
    from app.main_auth import app as auth_app

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

    id: int  # auth 库稳定 int：业务行 FK 引用此值
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
    schema-per-test），故该用户 id 以 1 起始且本测内稳定；返回 token 供把该用户作为
    "current 登录身份"发起业务 HTTP（须 seam 支持跨库裁决，或业务 local seam 直读）。
    """
    from app.modules.auth.models import Profile, User
    from app.modules.auth.security import create_access_token, hashpwd

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
            user_id=int(user.id),
            account_level=str(user.account_level),
            role=role,
            token_version=user.token_version,
        )
    return AuthUser(id=int(user.id), username=username, account_level=account_level, token=token or "")


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
    - grant_via_seam：升权写替身 → carrier 上 service_authz 原语。
    """
    from app.core.config import settings as _cfg
    from app.modules.auth import user_http as uh

    monkeypatch.setattr(_cfg, "auth_http_url", "http://auth-realm-test")
    monkeypatch.setattr(_cfg, "auth_http_token", "internal-test-secret")
    monkeypatch.setattr(_cfg, "auth_http_timeout_s", 1.0)

    async def _authz(*, user_id: int, **_: object) -> dict[str, object]:
        from sqlalchemy import select

        from app.modules.auth.models import Profile, User

        state: dict[str, object] = {
            "ok": False,
            "cause": None,
            "account_level": None,
            "role": None,
        }
        u = (
            await carrier.execute(select(User).where(User.id == int(user_id)))
        ).scalar_one_or_none()
        if u is None:
            state["cause"] = "not_found"
            return state
        now = now_iso()
        if u.is_locked and u.locked_until and u.locked_until > now:
            state["cause"] = "locked"
            return state
        prof = (
            await carrier.execute(
                select(Profile).where(Profile.user_id == int(user_id))
            )
        ).scalar_one_or_none()
        state["ok"] = True
        state["account_level"] = u.account_level
        state["role"] = prof.role if prof else "member"
        return state

    async def _fetch(user_id: int) -> Any:
        from sqlalchemy import select

        from app.modules.auth.models import Profile, User
        from app.modules.auth.snapshot import UserSnapshot, _snap_to_dict

        u = (
            await carrier.execute(select(User).where(User.id == int(user_id)))
        ).scalar_one_or_none()
        if u is None:
            return None, None
        p = (
            await carrier.execute(
                select(Profile).where(Profile.user_id == int(user_id))
            )
        ).scalar_one_or_none()
        snap = UserSnapshot(
            user_id=int(u.id),
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

    async def _grant(*, kind: str, user_id: int, **kw: object) -> int:
        from app.modules.auth import service_authz

        if kind == "incubation":
            return await service_authz.grant_incubation(carrier, int(user_id))
        return await service_authz.grant_exam_unlock(
            carrier,
            int(user_id),
            unlock_level=kw.get("unlock_level"),
            unlock_role=kw.get("unlock_role"),
        )

    monkeypatch.setattr(uh, "authorize_via_seam", _authz)
    monkeypatch.setattr(uh, "fetch_user_http_payload", _fetch)
    monkeypatch.setattr(uh, "grant_via_seam", _grant)


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
# app/db/session.py 与 app/db/auth_session.py 各有一枚模块级惰性 engine 单例：一旦在
# 某个测试的 event loop 内被 ``new_session()``/后台任务/未 override 的会话路径建立，就被
# 绑定到那个 loop。后续测试用新 loop 复用连接池时会抛 asyncpg
# 「got Future attached to a different loop」。每测后 dispose 两个单例，令各测试都在
# 自身 loop 内重建引擎，消除跨 loop 复用（conftest 自建的 schema 引擎为独立实例，不受影响）。
# ───────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
async def _reset_global_engines() -> AsyncGenerator[None]:
    yield
    from app.db.auth_session import dispose_auth_engine
    from app.db.session import dispose_engine

    await dispose_engine()
    await dispose_auth_engine()
