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
    "snapshot",
    "audit",
    "users",
    "verify_keys",
    "content_stats",
    "points_writer",
    "reports_writer",
    "notification_writer",
    "moderation_engine",
    "projects_read",
    "exam_read",
    "content_admin",
    "admin_guard",
    "feed_fanout",
    "interaction_read",
    "content_read",
    "content_write",
    "comment_api",
    "blog_api",
    "columns_api",
    "qa_read",
    "files_read",
)

_impls: dict[str, Any] = {}


class PortNotBound(RuntimeError):
    """端口未绑定：该进程没有装配对应实现（多半是漏调 boot.assemble()）。"""


class PortConflict(RuntimeError):
    """同一端口被绑定到两个不同实现：装配期失败，而不是静默覆盖。"""


def install(name: str, impl: Any) -> None:
    """绑定端口实现（由 auth.bootstrap / app.bootstrap 调用）。

    同名端口重复绑定**不同**实现即抛 :class:`PortConflict`：否则「谁生效取决于装配顺序」，
    与插件重名是同一类故障（鉴权端口被后装者覆盖 = 静默换实现，且很难在运行时发现）。
    绑定同一对象视为幂等，允许重复调用。
    """
    existing = _impls.get(name)
    if existing is not None and existing is not impl:
        raise PortConflict(
            f"端口 {name!r} 已被另一种实现绑定：拒绝覆盖（装配顺序不该决定实现）"
        )
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


def reset(name: str | None = None) -> None:
    """清空绑定（仅供测试隔离）：给 ``name`` 只清该端口，否则全清。

    单测替换某端口时用 ``reset("points_writer")`` 而不是全清：全清会让同一进程里
    其余已装配端口一起消失，测试顺序反而变成新的不变量。
    """
    if name is None:
        _impls.clear()
    else:
        _impls.pop(name, None)
