"""bot 面板 SSO 票据链路验收（面板并入社区后台：同域 ``/bot/`` + 免登）。

三层覆盖：
- **签发原语**（``auth.bot_sso.mint_ticket``）：claims 齐备（aud/type/account_level/jti/TTL），
  非 admin fail-closed 抛 ValueError。
- **auth 内部端点**（``/api/v1/auth/internal/bot-ticket``）：未配内部 token → 401（不成公网面）；
  配齐后 admin → 200 且票据可验；非 admin → 403（跨进程边界独立复核，不信任上游断言）。
- **业务端点**（``/api/v1/admin/bot/sso-ticket``）：无 admin cookie → 403；seam 未启用 → 403
  （后台 fail-closed 语义）；seam 开 + admin cookie → 200，票据 aud/type 正确且能被验签；
  seam 故障（内部读缝不可用）→ 503，绝不返回空票让前端以为「已免登」。
"""

import uuid

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth.router_bot_sso  # noqa: F401  # 确保 ROUTERS 已装好 bot-ticket 端点
from app.core.config import settings
from app.core.secrets import reveal
from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from auth import jwt_keys
from auth.bot_sso import BOT_SSO_AUD, BOT_SSO_TTL_SECONDS, mint_ticket
from auth.models import User
from tests.conftest import DB, Client, auth_user_uid

_INTERNAL_PATH = "/api/v1/auth/internal/bot-ticket"
_ADMIN_PATH = "/api/v1/admin/bot/sso-ticket"


def _internal_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _decode_ticket(ticket: str) -> dict[str, object]:
    """按签发算法验签（生产 RS256，本地/测试 HS256），校验 aud 后返回 payload。"""
    alg = jwt.get_unverified_header(ticket)["alg"]
    if alg == jwt_keys.RS256:
        key: object = jwt_keys.public_key()
    else:
        key = reveal(settings.jwt_secret)
    return jwt.decode(ticket, key, algorithms=[alg], audience=BOT_SSO_AUD)  # type: ignore[arg-type]


async def _mk_admin(auth_db: AsyncSession, uname: str) -> User:
    """在 auth realm 造一个 account_level=admin 账号，返回 ORM 行（供铸造后台 cookie）。"""
    au = await auth_user_uid(auth_db, username=uname, account_level="admin")
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


def _set_admin_cookie(client: Client, user: User) -> None:
    client.cookies.set(COOKIE_NAME, create_admin_access_token(user), path=COOKIE_PATH)


@pytest.fixture(autouse=True)
def _reset_internal_token():
    """每用例后清零 auth_http_token，避免跨用例污染内部缝开关。"""
    yield
    settings.auth_http_token = ""


# ─────────────────────── 签发原语 ───────────────────────


async def test_mint_ticket_claims_and_ttl() -> None:
    """票据 claims 齐备：aud/type/account_level/jti/sub，且 exp-iat == TTL。"""
    uid = uuid.uuid4()
    ticket, expires_in = mint_ticket(sub=str(uid), account_level="admin")
    assert expires_in == BOT_SSO_TTL_SECONDS
    payload = _decode_ticket(ticket)
    assert payload["aud"] == BOT_SSO_AUD
    assert payload["type"] == "bot_sso"
    assert payload["account_level"] == "admin"
    assert payload["sub"] == str(uid)
    assert payload["jti"]
    assert int(payload["exp"]) - int(payload["iat"]) == BOT_SSO_TTL_SECONDS


async def test_mint_ticket_rejects_non_admin() -> None:
    """非 admin 铸票直接拒（本票据只用于换 bot 面板管理员会话）。"""
    with pytest.raises(ValueError):
        mint_ticket(sub=str(uuid.uuid4()), account_level="normal")


async def test_mint_ticket_rejects_empty_subject() -> None:
    with pytest.raises(ValueError):
        mint_ticket(sub="  ", account_level="admin")


# ─────────────────────── auth 内部端点 ───────────────────────


async def test_internal_ticket_fail_closed_without_token(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部 token 未配置 → 一律 401（此端点不成为公网面）；错 token 同样 401。"""
    monkeypatch.setattr(settings, "auth_http_token", "")
    body = {"user_id": str(uuid.uuid4()), "account_level": "admin"}
    assert (await client.post(_INTERNAL_PATH, json=body)).status_code == 401
    assert (
        await client.post(_INTERNAL_PATH, json=body, headers=_internal_headers("nope"))
    ).status_code == 401


async def test_internal_ticket_issues_for_admin(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配齐内部 token + admin → 200，返回可验签的一次性票据。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    uid = uuid.uuid4()
    resp = await client.post(
        _INTERNAL_PATH,
        json={"user_id": str(uid), "account_level": "admin"},
        headers=_internal_headers("internal-secret-xyz"),
    )
    assert resp.status_code == 200
    payload = _decode_ticket(resp.json()["ticket"])
    assert payload["sub"] == str(uid)
    assert resp.json()["expires_in"] == BOT_SSO_TTL_SECONDS


async def test_internal_ticket_forbidden_for_non_admin(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 admin → 403（跨进程边界独立复核，不信任上游已裁决的断言）。"""
    monkeypatch.setattr(settings, "auth_http_token", "internal-secret-xyz")
    resp = await client.post(
        _INTERNAL_PATH,
        json={"user_id": str(uuid.uuid4()), "account_level": "normal"},
        headers=_internal_headers("internal-secret-xyz"),
    )
    assert resp.status_code == 403


# ─────────────────────── 业务端点 /admin/bot/sso-ticket ───────────────────────


async def test_admin_endpoint_rejects_without_cookie(client: Client) -> None:
    assert (await client.post(_ADMIN_PATH)).status_code == 403


async def test_admin_endpoint_fail_closed_when_seam_off(
    db: DB, client: Client, auth_db: AsyncSession
) -> None:
    """seam 未启用 → 即便带有效 admin cookie 也 403（后台一律 fail-closed）。"""
    _set_admin_cookie(client, await _mk_admin(auth_db, "bot_seam_off_admin"))
    assert (await client.post(_ADMIN_PATH)).status_code == 403


async def test_admin_endpoint_issues_ticket_for_admin(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """seam 开 + admin cookie → 200，票据 aud/type/sub 正确（可直接喂给 bot 面板）。"""
    admin = await _mk_admin(auth_db, "bot_ticket_admin")
    _set_admin_cookie(client, admin)

    resp = await client.post(_ADMIN_PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["expires_in"] == BOT_SSO_TTL_SECONDS
    payload = _decode_ticket(body["data"]["ticket"])
    assert payload["sub"] == str(admin.id)
    assert payload["type"] == "bot_sso"
    assert payload["account_level"] == "admin"


async def test_admin_endpoint_reports_unavailable_on_seam_failure(
    db: DB,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部铸票缝不可用 → 503（不返回空票；发不出票只是需要手动登录，但必须让运维看见）。"""
    from auth import user_http

    async def _boom(**_: object) -> dict[str, object]:
        raise user_http.UserHttpUnavailable("auth down")

    monkeypatch.setattr(user_http, "mint_bot_sso_ticket", _boom)
    _set_admin_cookie(client, await _mk_admin(auth_db, "bot_ticket_broken"))

    resp = await client.post(_ADMIN_PATH)
    assert resp.status_code == 503
