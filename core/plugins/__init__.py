"""
插件基础设施。

- :mod:`core.plugins.manifest` 清单（``manifest.toml``）的通用加载与形状校验

本包刻意**不认识任何业务概念**：清单里的 ``models`` / ``tasks`` 等字段如何被解释，
由调用方（当前是 ``app.modules.spec``）决定。这样 ``core`` 仍不依赖 ``app``，
而将来 ``core.plugins.host`` 可以对内部模块与外部 entry point 用同一套加载逻辑。
"""

from __future__ import annotations
