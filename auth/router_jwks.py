"""JWKS 发布端点（批 5，RFC 7517）。

AUTH 持私钥签发，验签方只需公钥。公钥经两条路分发：①部署期注入（主服务/APISIX 读
``LKM_JWT_PUBLIC_KEY``）；②运行期拉取本端点（标准 ``/.well-known/jwks.json`` 路径）。

该路径**不经** ``settings.api_prefix``（JWKS 的规范位置在站点根），故由 ``auth.main``
直接挂载。未配置密钥时返回空 ``keys`` 列表而非 404 —— 保持端点可探活，也让
「部署未启用 RS256」与「端点配错」两种情况可区分（前者 200 空集、后者 404）。
"""

from fastapi import APIRouter

from auth import jwt_keys

router = APIRouter(tags=["jwks"])


@router.get("/.well-known/jwks.json")
async def get_jwks() -> dict[str, object]:
    """当前 RSA 验签公钥集合（无密钥时为空集）。"""
    return jwt_keys.jwks_document()
