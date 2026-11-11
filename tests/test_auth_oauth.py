"""Tests for Github OAuth — service_oauth auth URL generation and callback logic."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import OAuthState, UserOAuth
from core.config import settings


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """oauth 域单测跑在 auth 独立库 schema（S5 拆后 oauth/user 表在 auth 库）。"""
    return auth_db


class TestGithubAuthUrl:
    async def should_contain_github_authorize_url(self, db: AsyncSession):
        from auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "github.com/login/oauth/authorize" in url

    async def should_contain_client_id(self, db: AsyncSession):
        from auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert f"client_id={settings.github_client_id}" in url

    async def should_contain_user_email_scope(self, db: AsyncSession):
        from auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "scope=user" in url
        assert "email" in url

    async def should_contain_redirect_uri(self, db: AsyncSession):
        from auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "redirect_uri=" in url

    async def should_generate_server_state_token(self, db: AsyncSession):
        from auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        # state is now server-generated; must be non-empty
        assert "&state=" in url
        # state should not be the empty placeholder
        state_idx = url.index("&state=") + len("&state=")
        state_value = url[state_idx:]
        if "&" in state_value:
            state_value = state_value.split("&")[0]
        assert len(state_value) > 0


class TestOAuthState:
    async def should_store_and_consume_state(self, db: AsyncSession):
        from auth.service_oauth import (
            consume_oauth_state,
            generate_oauth_state,
        )

        state = await generate_oauth_state(db, "login")
        assert len(state) > 0

        records = (
            (await db.execute(select(OAuthState).where(OAuthState.state == state)))
            .scalars()
            .all()
        )
        assert len(records) == 1
        assert not records[0].consumed

        await consume_oauth_state(db, state, "login")
        await db.refresh(records[0])
        assert records[0].consumed

    async def should_reject_already_consumed_state(self, db: AsyncSession):
        from auth.service_oauth import (
            consume_oauth_state,
            generate_oauth_state,
        )
        from core.err import BizError

        state = await generate_oauth_state(db, "login")
        await consume_oauth_state(db, state, "login")

        with pytest.raises(BizError):
            await consume_oauth_state(db, state, "login")

    async def should_reject_wrong_purpose(self, db: AsyncSession):
        from auth.service_oauth import (
            consume_oauth_state,
            generate_oauth_state,
        )
        from core.err import BizError

        state = await generate_oauth_state(db, "login")
        with pytest.raises(BizError):
            await consume_oauth_state(db, state, "bind")


class TestOauthRouterCallback:
    """Github 回调的 JSON 响应 —— 回调端点经 @respond 返回 ApiResp JSON（data 携带令牌/绑定结果）。"""

    async def should_return_access_and_refresh_tokens(self, db: AsyncSession):
        import json
        from unittest.mock import AsyncMock, patch

        from auth import router_oauth

        payload: dict[str, Any] = {
            "access_token": "acc123",
            "refresh_token": "ref123",
            "temp_token": None,
            "requires_2fa": False,
            "setup_required": False,
            "user_id": 1,
            "account_level": "normal",
        }

        with patch.object(
            router_oauth.service_oauth,
            "handle_github_callback",
            new=AsyncMock(return_value=payload),
        ):
            resp = await router_oauth.github_callback(code="c", state="s", db=db)

        data = json.loads(resp.body.decode())["data"]
        assert data["access_token"] == "acc123"
        assert data["refresh_token"] == "ref123"

    async def should_return_temp_token_when_2fa(self, db: AsyncSession):
        import json
        from unittest.mock import AsyncMock, patch

        from auth import router_oauth

        payload: dict[str, Any] = {
            "access_token": None,
            "refresh_token": None,
            "temp_token": "tmp999",
            "requires_2fa": True,
            "setup_required": False,
            "user_id": 1,
            "account_level": "admin",
        }

        with patch.object(
            router_oauth.service_oauth,
            "handle_github_callback",
            new=AsyncMock(return_value=payload),
        ):
            resp = await router_oauth.github_callback(code="c", state="s", db=db)

        data = json.loads(resp.body.decode())["data"]
        assert data["temp_token"] == "tmp999"
        assert data["requires_2fa"] is True
        assert data["access_token"] is None

    async def should_bind_callback_returns_message(self, db: AsyncSession):
        import json
        from unittest.mock import AsyncMock, patch

        from auth import router_oauth

        with patch.object(
            router_oauth.service_oauth,
            "bind_github",
            new=AsyncMock(return_value={"message": "Github account bound"}),
        ):
            resp = await router_oauth.github_bind_callback(code="c", state="s", db=db)

        data = json.loads(resp.body.decode())["data"]
        assert data["message"] == "Github account bound"

    async def should_bind_callback_propagate_biz_error(self, db: AsyncSession):
        from unittest.mock import patch

        from auth import router_oauth
        from auth.errors import AuthErr
        from core.err import BizError

        async def _boom(db: AsyncSession, code: str, state: str) -> None:
            raise BizError(AuthErr.OAUTH_EMAIL_TAKEN)

        with (
            patch.object(router_oauth.service_oauth, "bind_github", new=_boom),
            pytest.raises(BizError) as exc,
        ):
            await router_oauth.github_bind_callback(code="c", state="s", db=db)

        assert exc.value.errcode == AuthErr.OAUTH_EMAIL_TAKEN


class TestOAuthEmailAutoBind:
    async def should_reject_github_login_when_email_already_registered(
        self, db: AsyncSession
    ):
        from unittest.mock import AsyncMock, patch

        from auth.errors import AuthErr
        from auth.models import User
        from auth.providers.github import GithubOAuth
        from auth.providers.oauth import OAuthUserInfo
        from auth.security import hashpwd
        from auth.service_oauth import (
            generate_oauth_state,
            handle_github_callback,
        )
        from core.err import BizError

        db.add(
            User(
                username="alice",
                email="alice@example.com",
                hashed_password=await hashpwd("secret123456"),
                account_level="normal",
            )
        )
        await db.flush()

        state = await generate_oauth_state(db, "login")

        with (
            patch.object(
                GithubOAuth,
                "exchange_code",
                new=AsyncMock(return_value="tok"),
            ),
            patch.object(
                GithubOAuth,
                "fetch_user",
                new=AsyncMock(
                    return_value=OAuthUserInfo(
                        provider_user_id="123",
                        provider_email="alice@example.com",
                        username="alice",
                    )
                ),
            ),
            pytest.raises(BizError) as exc,
        ):
            await handle_github_callback(db, "code", state)

        assert exc.value.errcode == AuthErr.OAUTH_EMAIL_ALREADY_REGISTERED
        # 负向断言：未创建任何 OAuth 绑定（自动绑定确已移除）
        bindings = (await db.execute(select(UserOAuth))).scalars().all()
        assert bindings == []


class TestDesktopRedirect:
    """GitHub OAuth 的桌面 loopback 回跳：只允许回环地址 + PKCE 承诺。"""

    def should_accept_loopback_redirects(self):
        from auth.service_oauth import normalize_desktop_redirect

        assert (
            normalize_desktop_redirect("http://127.0.0.1:4567/cb")
            == "http://127.0.0.1:4567/cb"
        )
        assert (
            normalize_desktop_redirect("http://localhost:4567/cb")
            == "http://localhost:4567/cb"
        )
        assert normalize_desktop_redirect(None) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/cb",
            "http://127.0.0.1.evil.example/cb",
            "http://user:pass@127.0.0.1:4567/cb",
            "http://127.0.0.1:4567/cb#fragment",
            "file:///tmp/cb",
        ],
    )
    def should_reject_non_loopback_redirects(self, url: str):
        from auth.service_oauth import normalize_desktop_redirect
        from core.err import BizError

        with pytest.raises(BizError):
            normalize_desktop_redirect(url)

    def should_require_a_sha256_challenge_with_redirect(self):
        import hashlib

        from auth.service_oauth import normalize_desktop_challenge
        from core.err import BizError

        challenge = hashlib.sha256(b"verifier-1234567890").hexdigest()
        redirect = "http://127.0.0.1:4567/cb"
        assert normalize_desktop_challenge(challenge.upper(), redirect) == challenge
        assert normalize_desktop_challenge(None, None) is None
        with pytest.raises(BizError):
            normalize_desktop_challenge(None, redirect)
        with pytest.raises(BizError):
            normalize_desktop_challenge("not-a-hash", redirect)

    async def should_store_redirect_and_challenge_on_state(self, db: AsyncSession):
        from auth.service_oauth import generate_oauth_state

        state = await generate_oauth_state(
            db,
            "login",
            desktop_redirect="http://127.0.0.1:4567/cb",
            desktop_challenge="a" * 64,
        )
        row = (
            await db.execute(select(OAuthState).where(OAuthState.state == state))
        ).scalar_one()
        assert row.desktop_redirect == "http://127.0.0.1:4567/cb"
        assert row.desktop_challenge == "a" * 64

    async def should_exchange_code_once_with_the_right_verifier(
        self, db: AsyncSession
    ):
        import hashlib
        import uuid

        from auth.service_oauth import create_desktop_code, exchange_desktop_code
        from core.err import BizError

        verifier = "verifier-1234567890-abcdef"
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        user_id = uuid.uuid4()
        result = {
            "access_token": "acc",
            "refresh_token": "ref",
            "user_id": user_id,
            "account_level": "normal",
        }
        code = await create_desktop_code(db, result, challenge)

        with pytest.raises(BizError):
            await exchange_desktop_code(db, code, "wrong-verifier-123456")

        # 错误 verifier 不消耗 code，正确 verifier 仍可兑换；UUID 已转字符串。
        assert await exchange_desktop_code(db, code, verifier) == {
            **result,
            "user_id": str(user_id),
        }

        with pytest.raises(BizError):
            await exchange_desktop_code(db, code, verifier)

    def should_append_query_without_fragment(self):
        from auth.service_oauth import desktop_redirect_url

        assert (
            desktop_redirect_url(
                "http://127.0.0.1:4567/cb?state=x", code="c1"
            )
            == "http://127.0.0.1:4567/cb?state=x&code=c1"
        )
        assert "error=no+good" in desktop_redirect_url(
            "http://127.0.0.1:4567/cb", error="no good"
        )
