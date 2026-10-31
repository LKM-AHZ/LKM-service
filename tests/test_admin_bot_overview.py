"""``GET /admin/bot/overview``：权限门禁、未配置降级、bot 取数聚合与失败降级。"""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.admin.bot_admin_router as bot_admin_router
from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from auth.models import Profile, User
from core.config import settings
from tests.conftest import DB, Client

_PERM = "admin.bot_panel"


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """admin 与 auth 表同库（融合装配）。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam(auth_seam_fused: None) -> None:
    """require_admin 经 seam 裁决到本测 auth 表。"""


async def _mk_admin(db: DB, username: str = "bot_overview_admin") -> User:
    user = User(
        username=username,
        account_level="admin",
        hashed_password="bot-overview-placeholder-not-real",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="super_admin"))
    await db.flush()
    return user


async def _grant(db: DB, *perms: str) -> None:
    for perm in perms:
        db.add(RolePermission(role_name="admin:super_admin", permission=perm))
    await db.flush()


def _set_cookie(client: Client, user: User) -> None:
    client.cookies.set(COOKIE_NAME, create_admin_access_token(user), path=COOKIE_PATH)


class _FakeBotClient:
    """仅满足 ``async with`` 协议的假客户端（真实 HTTP 由 ``_bot_get`` 替身接管）。"""

    async def __aenter__(self) -> "_FakeBotClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def patch(self, _path: str, json: dict[str, object]) -> "_FakeResponse":
        return _FakeResponse(json)


class _FakeResponse:
    """假 httpx.Response：仅实现 raise_for_status / json。"""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return {"status": "ok", "message": "", "data": self._payload}


def _configure(monkeypatch: pytest.MonkeyPatch, *, base: str, key: str) -> None:
    monkeypatch.setattr(settings, "bot_api_base_url", base)
    monkeypatch.setattr(settings, "bot_admin_api_key", key)


async def test_overview_requires_bot_panel_permission(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)  # 未授 admin.bot_panel
    _set_cookie(client, admin)

    resp = await client.get("/api/v1/admin/bot/overview")

    assert resp.status_code == 403


async def test_overview_reports_not_configured(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="", key="")

    resp = await client.get("/api/v1/admin/bot/overview")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["configured"] is False
    assert data["available"] is False
    assert data["reason"] == "not_configured"


async def test_overview_aggregates_bot_data(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="http://lkmbot:6185/api/v1", key="service-key")
    monkeypatch.setattr(bot_admin_router, "_bot_client", lambda: _FakeBotClient())

    payloads = {
        "/stats": {
            "platform_count": 3,
            "plugin_count": 5,
            "message_count": 42,
            "cpu_percent": 12.5,
            "thread_count": 9,
            "memory": {"process": 128, "system": 4096},
            "running": {"days": 1, "hours": 2},
        },
        "/stats/version": {"version": "4.29.3", "dashboard_version": "4.29.3"},
        "/providers": {"providers": [{"id": "a"}, {"id": "b"}]},
        "/plugins": [{"name": "p1"}, {"name": "p2"}, {"name": "p3"}],
    }

    async def _fake_get(_client: object, path: str) -> object:
        return payloads[path]

    monkeypatch.setattr(bot_admin_router, "_bot_get", _fake_get)

    resp = await client.get("/api/v1/admin/bot/overview")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "configured": True,
        "available": True,
        "reason": None,
        "version": "4.29.3",
        "dashboard_version": "4.29.3",
        "platform_count": 3,
        "provider_count": 2,
        "plugin_count": 3,
        "message_count": 42,
        "cpu_percent": 12.5,
        "memory_process_mb": 128,
        "memory_system_mb": 4096,
        "thread_count": 9,
        "running": {"days": 1, "hours": 2},
    }


async def test_overview_degrades_when_bot_is_unreachable(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="http://lkmbot:6185/api/v1", key="service-key")
    monkeypatch.setattr(bot_admin_router, "_bot_client", lambda: _FakeBotClient())

    async def _fail(_client: object, _path: str) -> object:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bot_admin_router, "_bot_get", _fail)

    resp = await client.get("/api/v1/admin/bot/overview")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["configured"] is True
    assert data["available"] is False
    assert data["reason"] == "unavailable"
    assert data["version"] is None


async def test_logs_history_requires_bot_panel_permission(
    db: DB, client: Client
) -> None:
    admin = await _mk_admin(db)
    _set_cookie(client, admin)

    resp = await client.get("/api/v1/admin/bot/logs/history")

    assert resp.status_code == 403


async def test_logs_history_reports_not_configured(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="", key="")

    resp = await client.get("/api/v1/admin/bot/logs/history")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "configured": False,
        "available": False,
        "reason": "not_configured",
        "logs": [],
    }


async def test_logs_live_returns_503_when_not_configured(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="", key="")

    resp = await client.get("/api/v1/admin/bot/logs/live")

    assert resp.status_code == 503


async def test_pip_install_returns_503_when_not_configured(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="", key="")

    resp = await client.post(
        "/api/v1/admin/bot/pip/install", json={"package": "requests"}
    )

    assert resp.status_code == 503


async def test_platforms_normalizes_display_name(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="http://lkmbot:6185/api/v1", key="service-key")
    monkeypatch.setattr(bot_admin_router, "_bot_client", lambda: _FakeBotClient())

    async def _fake_get(_client: object, path: str) -> object:
        return {
            "/bots": {"bots": [{"id": "qa-bot", "type": "qq", "enable": True}]},
            "/bot-types": {"bot_types": [{"type": "qq", "display_name": "QQ"}]},
        }[path]

    monkeypatch.setattr(bot_admin_router, "_bot_get", _fake_get)

    resp = await client.get("/api/v1/admin/bot/platforms")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["configured"] is True and data["available"] is True
    assert data["items"] == [
        {"id": "qa-bot", "type": "qq", "display_name": "QQ", "enabled": True}
    ]


async def test_providers_normalizes_items(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="http://lkmbot:6185/api/v1", key="service-key")
    monkeypatch.setattr(bot_admin_router, "_bot_client", lambda: _FakeBotClient())

    async def _fake_get(_client: object, path: str) -> object:
        assert path == "/providers"
        return {
            "providers": [
                {
                    "id": "openai-main",
                    "provider_type": "chat_completion",
                    "model": "gpt-4o-mini",
                    "enable": True,
                }
            ]
        }

    monkeypatch.setattr(bot_admin_router, "_bot_get", _fake_get)

    resp = await client.get("/api/v1/admin/bot/providers")

    data = resp.json()["data"]
    assert data["items"] == [
        {
            "id": "openai-main",
            "type": "chat_completion",
            "model": "gpt-4o-mini",
            "enabled": True,
        }
    ]


async def test_plugins_and_knowledge_lists(
    db: DB, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _grant(db, _PERM)
    admin = await _mk_admin(db)
    _set_cookie(client, admin)
    _configure(monkeypatch, base="http://lkmbot:6185/api/v1", key="service-key")
    monkeypatch.setattr(bot_admin_router, "_bot_client", lambda: _FakeBotClient())

    async def _fake_get(_client: object, path: str) -> object:
        if path == "/plugins":
            return [{"name": "demo", "version": "1.2.3", "activated": True}]
        if path == "/knowledge-bases":
            return {"items": [{"kb_id": "kb-1", "kb_name": "Docs", "doc_count": 3}]}
        raise AssertionError(path)

    monkeypatch.setattr(bot_admin_router, "_bot_get", _fake_get)

    plugins = (await client.get("/api/v1/admin/bot/plugins")).json()["data"]
    assert plugins["items"] == [
        {"id": "demo", "name": "demo", "subtitle": "1.2.3", "enabled": True}
    ]
    knowledge = (await client.get("/api/v1/admin/bot/knowledge")).json()["data"]
    assert knowledge["items"] == [
        {"id": "kb-1", "name": "Docs", "subtitle": "3 docs", "enabled": None}
    ]
