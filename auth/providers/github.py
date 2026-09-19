"""Github OAuth 实现。"""

from typing import Any, cast

import httpx

from app.core.config import settings
from app.core.err import BizError
from app.core.secrets import reveal
from auth.errors import AuthErr
from auth.providers.oauth import OAuthUserInfo, register_provider

# GitHub API 调用超时：避免慢 Provider / 挂起拖住 OAuth 回调请求
_HTTP_TIMEOUT = httpx.Timeout(10.0)


def _http_error_to_biz(exc: httpx.HTTPError) -> BizError:
    """把 httpx 的 HTTP/网络/超时异常归一化为 OAUTH_PROVIDER_ERROR，勿泄漏 500。"""
    return BizError(AuthErr.OAUTH_PROVIDER_ERROR, f"GitHub request failed: {exc}")


class GithubOAuth:
    name: str = "github"

    def authorize_url(self, state: str) -> str:
        return (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&redirect_uri={settings.github_redirect_uri}"
            f"&scope=user:email"
            f"&state={state}"
        )

    async def exchange_code(self, code: str) -> str:
        """将 OAuth 授权码兑换为 GitHub 访问令牌。"""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": settings.github_client_id,
                        "client_secret": reveal(settings.github_client_secret),
                        "code": code,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                access_token = data.get("access_token")
                if not access_token:
                    raise BizError(
                        AuthErr.OAUTH_PROVIDER_ERROR,
                        data.get("error_description", "No access token"),
                    )
                return access_token
        except httpx.HTTPError as exc:
            raise _http_error_to_biz(exc) from None

    async def fetch_user(self, access_token: str) -> OAuthUserInfo:
        """获取 GitHub 用户资料和主邮箱。"""
        try:
            headers = {"Authorization": f"token {access_token}"}
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                user_resp = await client.get(
                    "https://api.github.com/user", headers=headers
                )
                user_resp.raise_for_status()
                user_data = user_resp.json()
                if "id" not in user_data:
                    raise BizError(
                        AuthErr.OAUTH_PROVIDER_ERROR, "Failed to fetch GitHub user"
                    )

                emails_resp = await client.get(
                    "https://api.github.com/user/emails", headers=headers
                )
                emails_resp.raise_for_status()
                emails_data = emails_resp.json()
                primary_email: str | None = None
                if isinstance(emails_data, list):
                    entries = cast(list[Any], emails_data)
                    for entry in entries:
                        if entry.get("primary") and entry.get("verified"):
                            primary_email = entry["email"]
                            break
                    if primary_email is None:
                        for entry in entries:
                            if entry.get("verified"):
                                primary_email = entry["email"]
                                break

                return OAuthUserInfo(
                    provider_user_id=str(user_data["id"]),
                    provider_email=primary_email,
                    username=user_data.get("login", ""),
                )
        except httpx.HTTPError as exc:
            raise _http_error_to_biz(exc) from None


register_provider(GithubOAuth())
