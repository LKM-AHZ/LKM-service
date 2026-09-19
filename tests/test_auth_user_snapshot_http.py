"""B1.2 seam（auth_http_url config-gated）单用户快照 HTTP 化的 TDD 验收。

范围：auth/snapshot.get_user_snapshot 的「miss 回填源」在 flag 开/关两态下的行为。

- (a) flag 默认空（auth_http_url="") → **in-monolith/DB/cache 原路径**：不碰 HTTP，走
  既有 A6 直读 DB + 缓存回填。回归锚=既有 A6 测试全绿前提下，这里显式断言
  ``user_http.enabled() is False`` 且 seam 返回 DB 的值并回填缓存。
- (b) flag 配齐（url+token）→ **client（httpx transport）被用**：
    · 成功 transport：seam 返回 wire 上的快照 + 以 wire 带出的真实 sv 回填缓存（证明取 HTTP
      而非就地 DB）。
    · 失败 transport（网络／500）→ seam **fail-open 回落本进程 DB** 返回 DB 值、不抛。
    · 权威 404 → seam 返回 None、不回落 DB、不缓存缺行。
- 附带：internal 读端点须内部令牌鉴权（未配置/错/缺 token → 401；正确 → 200 且零 PII）——
  确认 seam in 公共 blast 面不成立。HTTP 用 ``httpx.MockTransport`` 注入，别连真实网络。
fixture 复用 repo fakeredis 范式（reset _core + settings.redis_url + Redis.from_url→fake）。
"""
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
import app.core.user_cache as uc
import auth.snapshot as snap_mod
import auth.user_http as user_http
from app.core.config import settings
from auth.models import Profile, User
from auth.security import hashpwd
from auth.snapshot import (
    UserSnapshot,
    get_user_snapshot,
    get_user_snapshot_batch,
)
from tests.conftest import DB, Client


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """snapshot miss 回填/候补 DB 读取在 auth 面落真实 user。"""
    return auth_db


# wire 上的 user_id 是字符串（JSON 无 uuid 类型）；固定 uuid7 形态常量便于跨断言复用
_WIRE_UID = "00000000-0000-7000-8000-000000000001"


def _sv(uid: uuid.UUID) -> int:
    """由 uuid 派生的确定性「来源版本」，替代旧 int id 上的 ``900 + uid``。"""
    return 900 + uid.int % 1000


WIRE_SNAP = {
    "user_id": _WIRE_UID,
    "username": "wirebob",
    "display_name": "Wire Bob",
    "avatar": "avatars/w.png",
    "role": "member",
    "account_level": "normal",
    "banned": False,
    "nickname": "Wire Bob",
}


@pytest.fixture(autouse=True)
async def reset_globals() -> AsyncIterator[None]:
    """每用例前后复位 redis 单例 + seam 的 _client_factory 注入，杜绝跨用例残留。"""
    await redis_mod.close_redis()
    redis_mod._client = None  # type: ignore[attr-defined]
    redis_mod._client_pool = None  # type: ignore[attr-defined]
    user_http._client_factory = None  # type: ignore[attr-defined]
    yield
    await redis_mod.close_redis()
    redis_mod._client = None  # type: ignore[attr-defined]
    redis_mod._client_pool = None  # type: ignore[attr-defined]
    user_http._client_factory = None  # type: ignore[attr-defined]


def _enable_fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))


def _enable_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """配齐 url+token：seam 打开。"""
    monkeypatch.setattr(settings, "auth_http_url", "http://auth-proc")
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    monkeypatch.setattr(settings, "auth_http_timeout_s", 1.0)


async def _mk_user(db: AsyncSession, username: str, *, nickname: str) -> uuid.UUID:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secret123456"),
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, nickname=nickname, role="member"))
    await db.flush()
    return user.id


def _inject_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """用 httpx.MockTransport 替换 seam client 出站，离线端到端驱动（真 httpx 解析）。"""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        user_http,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=transport, base_url="http://auth-proc"),
    )


# ---- (a) 默认关闭：seam 不参与，in-monolith DB/cache 原路径回归锚 ----
class TestFlagOffDefault:
    async def test_empty_url_in_monolith_db_path_and_cache(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_url", "")
        monkeypatch.setattr(settings, "auth_http_token", "")
        assert user_http.enabled() is False  # 默认关

        _enable_fake_redis(monkeypatch)
        uid = await _mk_user(db, "offlocal", nickname="DB Local")
        snap = await get_user_snapshot(db, user_id=uid)  # 不联网，直接 DB miss → 回填
        assert snap is not None
        assert snap.display_name == "DB Local"  # 来自 DB
        assert snap.username == "offlocal"
        # 已回填缓存（原有 A6 行为保留）
        assert await uc.read_snap(uid) is not None
        sv, data = await uc.read_snap_with_version(uid)
        assert data is not None
        assert data["display_name"] == "DB Local"
        assert sv is not None


# ---- (b) flag 配齐：httpx transport 被使用 ----
class TestFlagOnUsesTransport:
    async def test_success_transport_returns_and_caches_wire_value(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB 也建同名用户，但 wire 给不同 display_name → 返回必须来自 wire（证真走 HTTP）。
        """
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        uid = await _mk_user(db, "wirelocal", nickname="DB name must NOT win")

        captured: list[httpx.Request] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": WIRE_SNAP, "sv": 555})

        _inject_transport(monkeypatch, _handler)
        snap = await get_user_snapshot(db, user_id=uid)
        assert snap is not None
        assert snap.display_name == "Wire Bob"  # wire 值胜出 → 确系跨 HTTP 取水
        # 请求带内部令牌、打到内部读端点
        assert captured and captured[0].url.path.endswith(
            f"/api/v1/auth/internal/users/{uid}/snapshot"
        )

        # 以 wire 带出的真实 sv 回填缓存
        sv, data = await uc.read_snap_with_version(uid)
        assert sv == 555
        assert data is not None and data["display_name"] == "Wire Bob"
        # 命中缓存再读 → 仍是 wire 快照
        snap2 = await get_user_snapshot(db, user_id=uid)
        assert snap2 is not None and snap2.display_name == "Wire Bob"

    async def test_connect_error_fails_open_to_db(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """transport 抛网络错/超时 → seam 回落本地 DB、返回 DB 值、不抛、不把失败当 None。
        """
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        uid = await _mk_user(db, "failopen", nickname="From DB after AUTH down")

        async def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("auth unreachable")

        _inject_transport(monkeypatch, _boom)
        snap = await get_user_snapshot(db, user_id=uid)  # 不抛
        assert snap is not None
        assert snap.display_name == "From DB after AUTH down"  # fail-open 到 DB

    async def test_http_500_fails_open_to_db(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        uid = await _mk_user(db, "status500", nickname="Kept from DB")

        async def _err500(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        _inject_transport(monkeypatch, _err500)
        snap = await get_user_snapshot(db, user_id=uid)
        assert snap is not None and snap.display_name == "Kept from DB"

    async def test_authoritative_404_returns_none_no_db_fallback(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AUTH 权威 404（用户不存在）→ seam 返回 None，不回落 DB、不缓存缺行。"""
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)

        async def _notfound(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={})

        _inject_transport(monkeypatch, _notfound)
        missing = uuid.uuid4()
        snap = await get_user_snapshot(db, user_id=missing)
        assert snap is None
        assert await uc.read_snap(missing) is None  # 没有缓存缺行


# ---- internal 读端点（公网 blast 面检查）：令牌鉴权 ----
class TestInternalEndpointAuth:
    @pytest.fixture(autouse=True)
    async def _bind_internal_auth_session(self, db: DB):
        """内部读端点 S5 拆库后走 get_auth_session（auth 库），覆盖到本测 auth schema。"""
        from app.main import app as _app
        from auth.db.session import get_auth_session

        async def _override() -> AsyncIterator[AsyncSession]:
            yield db

        _app.dependency_overrides[get_auth_session] = _override
        try:
            yield
        finally:
            _app.dependency_overrides.pop(get_auth_session, None)

    @pytest.fixture
    async def snap_user(self, db: DB) -> uuid.UUID:
        return await _mk_user(db, "epuser", nickname="EP")

    async def test_unconfigured_token_401(
        self,
        client: Client,
        snap_user: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "")
        resp = await client.get(
            f"/api/v1/auth/internal/users/{snap_user}/snapshot",
            headers={"Authorization": "Bearer whatever"},
        )
        assert resp.status_code == 401

    async def test_wrong_or_missing_token_401(
        self,
        client: Client,
        snap_user: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        resp = await client.get(
            f"/api/v1/auth/internal/users/{snap_user}/snapshot",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        resp2 = await client.get(f"/api/v1/auth/internal/users/{snap_user}/snapshot")
        assert resp2.status_code == 401

    async def test_valid_token_returns_frozen_fields_no_pii(
        self,
        client: Client,
        snap_user: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        resp = await client.get(
            f"/api/v1/auth/internal/users/{snap_user}/snapshot",
            headers={"Authorization": "Bearer real-token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sv"] is None or isinstance(body["sv"], int)
        assert set(body["data"]) == set(UserSnapshot.__dataclass_fields__)
        assert body["data"]["user_id"] == str(snap_user)
        # 零 PII 泄漏：无 email/phone/凭证
        assert "email" not in body["data"]
        assert "phone" not in body["data"]
        assert "hashed_password" not in body["data"]


# ---- (c) M6.5 by-ids 批量跨 AUTH 读：一次 HTTP、分块、缓存前置、缺行/故障语义 ----


def _batch_handler(
    calls: list[httpx.Request], snapshot_by_id: dict[uuid.UUID, dict[str, Any]]
) -> Any:
    """by-ids 假端点：按请求里的 ids 回 items（未知 id 回 data=null）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        ids = [
            uuid.UUID(p)
            for p in request.url.params.get("ids", "").split(",")
            if p.strip()
        ]
        items = [
            {
                "user_id": str(uid),
                "data": snapshot_by_id.get(uid),
                "sv": _sv(uid) if uid in snapshot_by_id else None,
            }
            for uid in ids
        ]
        return httpx.Response(200, json={"items": items})

    return _handler


class TestBatchByIds:
    async def test_many_ids_single_http_call(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N 个 id 且缓存全 miss → **恰好 1 次** HTTP（消逐 id 循环），并按 wire sv 回填缓存。"""
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        ids = [await _mk_user(db, f"b{i}", nickname=f"N{i}") for i in range(5)]
        calls: list[httpx.Request] = []
        by_id = {
            uid: {**WIRE_SNAP, "user_id": str(uid), "nickname": f"W{uid}"}
            for uid in ids
        }
        _inject_transport(monkeypatch, _batch_handler(calls, by_id))

        snaps = await get_user_snapshot_batch(db, user_ids=ids)

        assert set(snaps) == set(ids)
        assert snaps[ids[0]].nickname == f"W{ids[0]}"  # 值来自 wire 而非 DB
        assert len(calls) == 1
        assert calls[0].url.path.endswith("/api/v1/auth/internal/users/by-ids")
        assert calls[0].headers["Authorization"] == "Bearer internal-secret-xyz"
        sv, data = await uc.read_snap_with_version(ids[0])
        assert sv == _sv(ids[0])
        assert data is not None and data["nickname"] == f"W{ids[0]}"

    async def test_all_cached_makes_zero_http_calls(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存全命中 → 零 HTTP（批量缓存读前置）。"""
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        ids = [await _mk_user(db, f"c{i}", nickname=f"C{i}") for i in range(3)]
        for uid in ids:
            assert await uc.write_if_newer(
                uid, {**WIRE_SNAP, "user_id": str(uid)}, _sv(uid), 0
            )
        calls: list[httpx.Request] = []
        _inject_transport(monkeypatch, _batch_handler(calls, {}))

        snaps = await get_user_snapshot_batch(db, user_ids=ids)

        assert set(snaps) == set(ids)
        assert calls == []

    async def test_batch_chunked_by_max_ids(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N 个 id 时 HTTP 调用数 = ⌈N/上限⌉（超限自动分批，不超发单次请求）。"""
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        monkeypatch.setattr(snap_mod, "BATCH_IDS_MAX", 2)
        ids = [await _mk_user(db, f"d{i}", nickname=f"D{i}") for i in range(5)]
        calls: list[httpx.Request] = []
        by_id = {uid: {**WIRE_SNAP, "user_id": str(uid)} for uid in ids}
        _inject_transport(monkeypatch, _batch_handler(calls, by_id))

        snaps = await get_user_snapshot_batch(db, user_ids=ids)

        assert set(snaps) == set(ids)
        assert len(calls) == 3  # ⌈5/2⌉
        for call in calls:
            sent = [p for p in call.url.params.get("ids", "").split(",") if p]
            assert len(sent) <= 2

    async def test_batch_http_failure_skips_rows_without_raising(
        self, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 不可用 → 整批跳过（空结果）、不抛、不回落业务 DB（跨 realm 无本地回退）。"""
        _enable_fake_redis(monkeypatch)
        _enable_seam(monkeypatch)
        ids = [await _mk_user(db, "e0", nickname="E0")]

        async def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("auth unreachable")

        _inject_transport(monkeypatch, _boom)
        snaps = await get_user_snapshot_batch(db, user_ids=ids)
        assert snaps == {}  # 缺行跳过 ≠ 故障抛出
        assert await uc.read_snap(ids[0]) is None  # 没有把失败当值缓存


class TestBatchEndpoint:
    @pytest.fixture(autouse=True)
    async def _bind_internal_auth_session(self, db: DB):
        from app.main import app as _app
        from auth.db.session import get_auth_session

        async def _override() -> AsyncIterator[AsyncSession]:
            yield db

        _app.dependency_overrides[get_auth_session] = _override
        try:
            yield
        finally:
            _app.dependency_overrides.pop(get_auth_session, None)

    async def _get(self, client: Client, ids: str, token: str = "real-token"):
        return await client.get(
            "/api/v1/auth/internal/users/by-ids",
            params={"ids": ids},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def test_returns_items_for_all_ids_including_missing(
        self, client: Client, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        uid = await _mk_user(db, "batchep", nickname="BE")
        missing = uuid.uuid4()
        resp = await self._get(client, f"{uid},{missing}")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["user_id"] for i in items] == [str(uid), str(missing)]
        assert set(items[0]["data"]) == set(UserSnapshot.__dataclass_fields__)
        assert items[0]["data"]["user_id"] == str(uid)
        assert items[1]["data"] is None and items[1]["sv"] is None  # 缺行语义同单条
        assert "email" not in items[0]["data"]

    async def test_requires_internal_token(
        self, client: Client, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        assert (await self._get(client, _WIRE_UID, token="wrong")).status_code == 401
        monkeypatch.setattr(settings, "auth_http_token", "")
        assert (await self._get(client, _WIRE_UID)).status_code == 401

    async def test_bad_ids_rejected(
        self, client: Client, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        assert (await self._get(client, "abc")).status_code == 400
        assert (await self._get(client, "")).status_code == 400
        assert (await self._get(client, "0")).status_code == 400

    async def test_over_limit_rejected_not_truncated(
        self, client: Client, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超限直接 400（截断=静默错答案）。"""
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        monkeypatch.setattr(snap_mod, "BATCH_IDS_MAX", 2)
        three = ",".join(
            (
                "00000000-0000-7000-8000-000000000001",
                "00000000-0000-7000-8000-000000000002",
                "00000000-0000-7000-8000-000000000003",
            )
        )
        resp = await self._get(client, three)
        assert resp.status_code == 400
        assert "too many ids" in resp.json()["detail"]

    async def test_duplicate_ids_deduped(
        self, client: Client, db: DB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "auth_http_token", "real-token")
        uid = await _mk_user(db, "dupids", nickname="DUP")
        resp = await self._get(client, f"{uid},{uid},{uid}")
        assert resp.status_code == 200
        assert [i["user_id"] for i in resp.json()["items"]] == [str(uid)]
