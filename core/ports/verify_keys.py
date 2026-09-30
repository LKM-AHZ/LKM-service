"""验签公钥端口：JWKS 拉取/刷新、验签状态与 passkey 挑战清理。

实现由 auth 绑定（私钥与 JWKS 端点都在 auth 域）。
"""

from __future__ import annotations

from core.ports.registry import get


async def refresh_verify_key() -> bool:
    """主动从 auth 的 /jwks 刷新验签公钥；返回是否拿到可用公钥。"""
    return bool(await get("verify_keys").refresh_verify_key())


def verify_key_status() -> str:
    """验签公钥的三档状态（``"ok"`` 表示本地/缓存已有可用公钥），供 readiness 上报。"""
    return str(get("verify_keys").verify_key_status())


async def start_verify_key_refresh() -> None:
    """启动后台公钥周期刷新 task。"""
    await get("verify_keys").start_verify_key_refresh()


async def stop_verify_key_refresh() -> None:
    """停止后台公钥刷新 task（收尾用）。"""
    await get("verify_keys").stop_verify_key_refresh()


async def cleanup_expired_challenges() -> None:
    """清理过期的 passkey 挑战记录（长驻 task 或一次性调用）。"""
    await get("verify_keys").cleanup_expired_challenges()
