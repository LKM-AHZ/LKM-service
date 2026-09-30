"""端口层：app / auth 之间的运行时能力通道。

每个子模块声明一组能力（纯转发到已绑定的实现），实现由 auth 侧 ``auth.bootstrap``
绑定、由 app 侧 ``app.bootstrap`` 绑定（如 content_stats）。本包不 import app/auth。
"""

from core.ports.registry import (
    REQUIRED_PORTS,
    PortNotBound,
    get,
    install,
    is_bound,
    reset,
    validate_all,
)

__all__ = [
    "REQUIRED_PORTS",
    "PortNotBound",
    "get",
    "install",
    "is_bound",
    "reset",
    "validate_all",
]
