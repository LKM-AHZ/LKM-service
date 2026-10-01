"""
端口注册表：app 与 auth 之间「调用能力」的唯一通道。
拆分前 app 直接 ``from auth.deps/seams/snapshot import ...``：这既让 app 依赖 auth 的实现，
也让两侧的 import 边界无法收口。现在 core 只声明**协议与包装函数**，真正的实现由 auth 在
自己的 bootstrap 里 :func:`install` 进来；app 只 import core。

**fail-fast**：未绑定的端口一经调用即抛 :class:`PortNotBound`，绝不静默降级为「放行/空结果」
（鉴权类端口若静默返回 None 就等于匿名放行）。装配根 ``boot.assemble()`` 末尾再调
:func:`validate_all` 做启动期兜底。
"""

from __future__ import annotations

from typing import Any

REQUIRED_PORTS: tuple[str, ...] = (
    "authz",
    "authz_session",
    "snapshot",
    "audit",
    "users",
    "verify_keys",
    "content_stats",
)

_impls: dict[str, Any] = {}


class PortNotBound(RuntimeError):
    """端口未绑定：该进程没有装配对应实现（多半是漏调 boot.assemble()）。"""


def install(name: str, impl: Any) -> None:
    """绑定端口实现（由 auth.bootstrap / app.bootstrap 调用）。"""
    _impls[name] = impl


def get(name: str) -> Any:
    """取出端口实现；未绑定即抛 :class:`PortNotBound`。"""
    try:
        return _impls[name]
    except KeyError:
        raise PortNotBound(
            f"端口 {name!r} 未绑定：该进程必须先完成装配"
            "（backend/worker 经 boot.assemble()，auth 进程经 auth.bootstrap.register()）"
        ) from None


def is_bound(name: str) -> bool:
    return name in _impls


def validate_all() -> None:
    """启动期校验：装配完整进程时所有必需端口都须已绑定。"""
    missing = [n for n in REQUIRED_PORTS if n not in _impls]
    if missing:
        raise PortNotBound(f"装配缺少端口实现: {missing}")


def reset() -> None:
    """清空绑定（仅供测试隔离）。"""
    _impls.clear()
