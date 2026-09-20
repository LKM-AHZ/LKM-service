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

import importlib
import uuid

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth.bot_sso as bot_sso
import auth.router_bot_sso  # noqa: F401  # 确保 ROUTERS 已装好 bot-ticket 端点
from app.core.config import settings
from app.core.secrets import reveal
from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from auth import jwt_keys
from auth.bot_sso import (
    BOT_SSO_ACCOUNT_LEVEL,
    BOT_SSO_AUD,
    BOT_SSO_ISSUER,
    BOT_SSO_TTL_SECONDS,
    BOT_SSO_TYPE,
    mint_ticket,
)
from auth.models import User
from tests.conftest import DB, Client, auth_user_uid

_INTERNAL_PATH = "/api/v1/auth/internal/bot-ticket"
_ADMIN_PATH = "/api/v1/admin/bot/sso-ticket"

#: 协议值的环境变量名（与 LKM-bot 消费侧、compose 的 x-bot-sso-env 锚点同名）。
_SSO_ENV_VARS = (
    "LKM_BOT_SSO_AUDIENCE",
    "LKM_BOT_SSO_TYPE",
    "LKM_BOT_SSO_ISSUER",
    "LKM_BOT_SSO_ACCOUNT_LEVEL",
    "LKM_BOT_SSO_TTL_SECONDS",
)


def _internal_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _decode_ticket(ticket: str, *, audience: str = BOT_SSO_AUD) -> dict[str, object]:
    """按签发算法验签（生产 RS256，本地/测试 HS256），校验 aud 后返回 payload。"""
    alg = jwt.get_unverified_header(ticket)["alg"]
    if alg == jwt_keys.RS256:
        key: object = jwt_keys.public_key()
    else:
        key = reveal(settings.jwt_secret)
    return jwt.decode(ticket, key, algorithms=[alg], audience=audience)  # type: ignore[arg-type]


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
    """票据 claims 齐备：iss/aud/type/account_level/jti/sub，且 exp-iat == TTL。

    claims 取自模块常量（不是字面量）：常量若被误改，这里与消费侧断言会一起红。
    """
    uid = uuid.uuid4()
    ticket, expires_in = mint_ticket(sub=str(uid), account_level=BOT_SSO_ACCOUNT_LEVEL)
    assert expires_in == BOT_SSO_TTL_SECONDS
    payload = _decode_ticket(ticket)
    assert payload["aud"] == BOT_SSO_AUD
    assert payload["iss"] == BOT_SSO_ISSUER
    assert payload["type"] == BOT_SSO_TYPE
    assert payload["account_level"] == BOT_SSO_ACCOUNT_LEVEL
    assert payload["sub"] == str(uid)
    assert payload["jti"]
    assert int(payload["exp"]) - int(payload["iat"]) == BOT_SSO_TTL_SECONDS


# ─────────────────────── 协议常量与环境变量覆盖 ───────────────────────


def test_protocol_defaults_are_pinned() -> None:
    """默认值即两侧（auth 签发 / bot 消费）与部署层（compose 锚点、.env.example）共用的值。

    跨仓/跨部署单元无法共享 import，故默认值只能靠**双方断言**锁死；改这里必须同步
    LKM-bot ``astrbot/lkm/sso.py`` 与 ``.env.example``（后者另有测试锁部署层）。
    """
    assert BOT_SSO_AUD == "lkm:bot"
    assert BOT_SSO_TYPE == "bot_sso"
    assert BOT_SSO_ISSUER == "lkm-auth"
    assert BOT_SSO_ACCOUNT_LEVEL == "admin"
    assert BOT_SSO_TTL_SECONDS == 60


def test_protocol_values_follow_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """五个协议值都可由同名环境变量覆盖，且签发票据随之变化（部署层注入的就是这组名字）。"""
    overrides = {
        "LKM_BOT_SSO_AUDIENCE": "lkm:bot-x",
        "LKM_BOT_SSO_TYPE": "bot_sso_x",
        "LKM_BOT_SSO_ISSUER": "auth-x",
        "LKM_BOT_SSO_ACCOUNT_LEVEL": "superadmin",
        "LKM_BOT_SSO_TTL_SECONDS": "30",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    reloaded = importlib.reload(bot_sso)
    try:
        assert reloaded.BOT_SSO_AUD == "lkm:bot-x"
        assert reloaded.BOT_SSO_TYPE == "bot_sso_x"
        assert reloaded.BOT_SSO_ISSUER == "auth-x"
        assert reloaded.BOT_SSO_ACCOUNT_LEVEL == "superadmin"
        assert reloaded.BOT_SSO_TTL_SECONDS == 30

        # 覆盖后的校验路径同样成立：非覆盖值被拒、覆盖值签发成功
        with pytest.raises(ValueError):
            reloaded.mint_ticket(sub=str(uuid.uuid4()), account_level="admin")
        ticket, ttl = reloaded.mint_ticket(
            sub=str(uuid.uuid4()), account_level="superadmin"
        )
        assert ttl == 30
        payload = _decode_ticket(ticket, audience="lkm:bot-x")
        assert payload["iss"] == "auth-x"
        assert payload["type"] == "bot_sso_x"
        assert payload["account_level"] == "superadmin"
    finally:
        for name in _SSO_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(bot_sso)


def test_ttl_env_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 配了非数字/非正值 → 回落默认 60s（宁可默认，也不签出荒谬有效期）。"""
    for raw in ("abc", "0", "-5"):
        monkeypatch.setenv("LKM_BOT_SSO_TTL_SECONDS", raw)
        try:
            assert importlib.reload(bot_sso).BOT_SSO_TTL_SECONDS == 60, raw
        finally:
            monkeypatch.delenv("LKM_BOT_SSO_TTL_SECONDS", raising=False)
            importlib.reload(bot_sso)


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
    assert payload["type"] == BOT_SSO_TYPE
    assert payload["account_level"] == BOT_SSO_ACCOUNT_LEVEL


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
