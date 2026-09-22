"""OAuth 提供商协议与注册表 —— 加新提供商 = 新增一个实现文件 + 注册一行。"""

from dataclasses import dataclass
from typing import Protocol

from app.core.err import BizError, CommonErr


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
        # 未注册的提供商名是**调用方给错了值**，不是上游故障：用 INVALID_INPUT(422) 而不是
        # OAUTH_PROVIDER_ERROR(502)——502 会被客户端/网关当成可重试的上游不可用（真上游失败
        # 才该是 502，见 providers/github）。同时不回显原始取值，避免透出内部词表。
        raise BizError(CommonErr.INVALID_INPUT, "Unknown OAuth provider")
    return provider
