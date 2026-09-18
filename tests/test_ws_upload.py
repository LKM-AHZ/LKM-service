"""WebSocket 上传登记事件相关测试。

覆盖：
1. ``_authorize``：合法 access token 解析出 user_id；缺/坏 token → None。
2. ``ConnectionManager`` 扇出：按 user_id 路由、非目标用户不收到、失效连接被清理。
3. ``broker``：通道命名约定 + Redis 不可用/发布异常时静默 no-op（fail-open）。

注：真实「登录→握手→收发广播」依赖 starlette TestClient 的 WebSocket，但当前
starlette 1.3 已转向 ``httpx2``（本项目未安装，测试栈用 ``ASGITransport`` 且不支持
WS upgrade），故握手拒绝路径以 ``_authorize`` 单测覆盖，扇出/发布用纯逻辑验证。
完整握手留待集成/真验。
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.auth.security import create_access_token
from app.ws import broker as ws_broker
from app.ws import router as ws_router
from app.ws.manager import ConnectionManager

# 稳定的 uuid7 形态用户 id（第 3 段以 7 开头、第 4 段以 8 开头）。
_UID1 = uuid.UUID("00000000-0000-7000-8000-000000000001")
_UID2 = uuid.UUID("00000000-0000-7000-8000-000000000002")
_UID3 = uuid.UUID("00000000-0000-7000-8000-000000000003")


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """ws authorize 校验需要 auth(user) 存在（融合；business 无 users）。"""
    return fused_db_session


async def _insert_user(db: AsyncSession, username: str = "wsuser") -> uuid.UUID:
    u = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        account_level="local",
        is_locked=False,
        failed_login_attempts=0,
        token_version=0,
    )
    db.add(u)
    await db.flush()
    await db.refresh(u)
    return u.id


class TestAuthorize:
    async def test_valid_token_returns_user_id(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uid = await _insert_user(db)
        token = create_access_token(user_id=uid, account_level="normal", role="member")

        async def _fake_new_session() -> AsyncSession:
            return db

        monkeypatch.setattr(ws_router, "new_session", _fake_new_session)
        assert await ws_router._authorize(token) == uid

    async def test_invalid_token_returns_none(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_new_session() -> AsyncSession:
            return db

        monkeypatch.setattr(ws_router, "new_session", _fake_new_session)
        assert await ws_router._authorize("garbage-token") is None

    async def test_missing_token_returns_none(self) -> None:
        assert await ws_router._authorize("") is None


class _FakeWS:
    """可记录 send_text 的假 WebSocket；opts dead=True 模拟失效连接。"""

    def __init__(self, *, dead: bool = False) -> None:
        self.sent: list[str] = []
        self._dead = dead

    async def send_text(self, data: str) -> None:
        if self._dead:
            raise RuntimeError("connection dead")
        self.sent.append(data)


class TestConnectionManagerFanout:
    async def test_dispatch_reaches_target_user_only(self) -> None:
        m = ConnectionManager()
        target = _FakeWS()
        other = _FakeWS()
        await m.register(_UID1, target)
        await m.register(_UID2, other)

        payload = json.dumps(
            {"event": "upload_registered", "upload_id": "abc123", "file": {}}
        )
        await m.dispatch(_UID1, ws_broker.CHANNEL_UPLOAD, payload)

        assert target.sent and "abc123" in target.sent[0]
        assert other.sent == []  # 其它用户不收到
        await m.close()

    async def test_dispatch_prunes_dead_connections(self) -> None:
        m = ConnectionManager()
        bad, good = _FakeWS(dead=True), _FakeWS()
        await m.register(_UID3, bad)
        await m.register(_UID3, good)

        await m.dispatch(_UID3, ws_broker.CHANNEL_UPLOAD, "payload")

        assert good.sent == ["payload"]  # 好连接仍收到
        async with m._lock:
            live = list(
                m._connections.get(_UID3, {}).get(ws_broker.CHANNEL_UPLOAD, ())
            )
        assert bad not in live  # 坏连接被清理
        assert good in live
        await m.close()


class TestBroker:
    def test_channel_naming(self) -> None:
        # M6.7 泛化后：ws:{user_id}:{channel}（原 ws:upload:{user_id}）
        assert ws_broker.upload_channel(_UID1) == f"ws:{_UID1}:upload"
        assert ws_broker.ws_channel(_UID1, "notify") == f"ws:{_UID1}:notify"

    async def test_publish_fail_open_without_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redis 不可用时发布静默 no-op，不抛异常
        async def _no_redis() -> None:
            return None

        monkeypatch.setattr(ws_broker, "get_redis", _no_redis)
        await ws_broker.publish_upload_bound(_UID1, {"event": "upload_registered"})

    async def test_publish_suppresses_broken_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redis 可用但 publish 抛错也应静默（不打断登记主流程）
        from redis.asyncio import Redis

        class _BrokenRedis(Redis):
            async def publish(self, *_a: Any, **_k: Any) -> int:  # type: ignore[override]
                raise ConnectionError("redis down")

        async def _fake_redis() -> Redis:
            return _BrokenRedis.from_url(
                "redis://localhost:6379/0", decode_responses=True
            )

        monkeypatch.setattr(
            ws_broker,
            "get_redis",
            _fake_redis,  # type: ignore[arg-type]
        )
        await ws_broker.publish_upload_bound(_UID1, {"event": "upload_registered"})
