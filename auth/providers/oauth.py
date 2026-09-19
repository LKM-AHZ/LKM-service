"""OAuth 提供商协议与注册表 —— 加新提供商 = 新增一个实现文件 + 注册一行。"""

from dataclasses import dataclass
from typing import Protocol

from app.core.err import BizError
from auth.errors import AuthErr


@dataclass(frozen=True)
class OAuthUserInfo:
    """提供商返回的归一化用户信息。"""

    provider_user_id: str
    provider_email: str | None
    username: str


class OAuthProvider(Protocol):
    """OAuth 提供商的三个关键动作。"""

    name: str

    def authorize_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> str: ...

    async def fetch_user(self, access_token: str) -> OAuthUserInfo: ...


_REGISTRY: dict[str, OAuthProvider] = {}


def register_provider(provider: OAuthProvider) -> None:
    if provider.name in _REGISTRY:
        raise ValueError(f"Duplicate OAuth provider: {provider.name}")
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> OAuthProvider:
    provider = _REGISTRY.get(name)
    if provider is None:
        raise BizError(AuthErr.OAUTH_PROVIDER_ERROR, f"Unknown OAuth provider: {name}")
    return provider
