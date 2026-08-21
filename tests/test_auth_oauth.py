"""Tests for Github OAuth — service_oauth auth URL generation and callback logic."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.auth.models import OAuthState, UserOAuth


class TestGithubAuthUrl:
    async def should_contain_github_authorize_url(self, db: AsyncSession):
        from app.modules.auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "github.com/login/oauth/authorize" in url

    async def should_contain_client_id(self, db: AsyncSession):
        from app.modules.auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert f"client_id={settings.github_client_id}" in url

    async def should_contain_user_email_scope(self, db: AsyncSession):
        from app.modules.auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "scope=user" in url
        assert "email" in url

    async def should_contain_redirect_uri(self, db: AsyncSession):
        from app.modules.auth.service_oauth import get_github_auth_url

        url = await get_github_auth_url(db)
        assert "redirect_uri=" in url

    async def should_generate_server_state_token(self, db: AsyncSession):
        from app.modules.auth.service_oauth import get_github_auth_url

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
        from app.modules.auth.service_oauth import (
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
        from app.core.err import BizError
        from app.modules.auth.service_oauth import (
            consume_oauth_state,
            generate_oauth_state,
        )

        state = await generate_oauth_state(db, "login")
        await consume_oauth_state(db, state, "login")

        with pytest.raises(BizError):
            await consume_oauth_state(db, state, "login")

    async def should_reject_wrong_purpose(self, db: AsyncSession):
        from app.core.err import BizError
        from app.modules.auth.service_oauth import (
            consume_oauth_state,
            generate_oauth_state,
        )

        state = await generate_oauth_state(db, "login")
        with pytest.raises(BizError):
            await consume_oauth_state(db, state, "bind")


class TestOauthRouterCallback:
    """Github 回调的 JSON 响应 —— 回调端点经 @respond 返回 ApiResp JSON（data 携带令牌/绑定结果）。"""

    async def should_return_access_and_refresh_tokens(self, db: AsyncSession):
        import json
        from unittest.mock import AsyncMock, patch

        from app.modules.auth import router_oauth

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

        from app.modules.auth import router_oauth

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

        from app.modules.auth import router_oauth

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

        from app.core.err import BizError
        from app.modules.auth import router_oauth
        from app.modules.auth.errors import AuthErr

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

        from app.core.err import BizError
        from app.db.models import User
        from app.modules.auth.errors import AuthErr
        from app.modules.auth.providers.github import GithubOAuth
        from app.modules.auth.providers.oauth import OAuthUserInfo
        from app.modules.auth.security import hashpwd
        from app.modules.auth.service_oauth import (
            generate_oauth_state,
            handle_github_callback,
        )

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
