"""
插件宿主：发现 → 校验 → 定序。

本模块**不认识业务概念**，只处理 :class:`~core.plugins.manifest.Manifest` 集合：

- :class:`PluginSet`：重名检测、``requires`` 闭包校验、拓扑定序（依赖先装）；
- :func:`discover_entry_points`：从已安装发行版的 ``lkm.plugins`` 入口点读取外部插件。

内置业务模块与外部插件走同一套清单，差别只在清单来源：内置来自
``app/modules/*/manifest.toml``，外部来自 ``entry_points(group="lkm.plugins")``。

**为什么校验必须 fail-fast**：``requires`` 写错或成环时，若只是「少装了某模块」，表现是
运行时缺命令/缺字段（404 或空字段），而不是启动失败——这类故障最难定位。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from core.plugins.manifest import Manifest

#: 外部插件入口点组名。插件包需暴露一个无参可调用对象，返回 :class:`Manifest`。
ENTRY_POINT_GROUP = "lkm.plugins"


class PluginError(ValueError):
    """插件集合非法：重名、``requires`` 无法满足、或依赖成环。"""


@dataclass(frozen=True)
class PluginSet:
    """一组插件清单。不可变视图，校验与定序都返回新结果、不改内部状态。"""

    manifests: tuple[Manifest, ...]

    def validate(self) -> None:
        """重名 / requires 闭包 / 成环，任一不满足即抛 :class:`PluginError`。"""
        names = [m.name for m in self.manifests]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise PluginError(f"插件重名：{duplicates}")

        provided = {cap for m in self.manifests for cap in m.provides}
        missing = {
            cap for m in self.manifests for cap in m.requires if cap not in provided
        }
        if missing:
            raise PluginError(
                f"requires 无法满足：{sorted(missing)}"
                f"（已提供的 provides：{sorted(provided)}）"
            )

        # 依赖成环：定序算法无法消耗完全部节点即说明有环。
        ordered = self.install_order()
        if len(ordered) != len(self.manifests):
            stuck = sorted(m.name for m in self.manifests if m not in ordered)
            raise PluginError(f"插件依赖成环：{stuck}")

    def install_order(self) -> tuple[Manifest, ...]:
        """按 ``requires`` 拓扑排序（被依赖者在前）；无依赖时保持输入顺序。

        实现是稳定的 Kahn 算法：每轮按输入顺序取「依赖已满足」的节点，因此内置模块
        （``requires`` 为空）的顺序与清单顺序完全一致——路由注册顺序不会被动改变。
        """
        provided_by: dict[str, list[str]] = {}
        for manifest in self.manifests:
            for cap in manifest.provides:
                provided_by.setdefault(cap, []).append(manifest.name)

        pending = list(self.manifests)
        done: list[Manifest] = []
        while pending:
            progressed = False
            remaining: list[Manifest] = []
            for manifest in pending:
                deps = {
                    name
                    for cap in manifest.requires
                    for name in provided_by.get(cap, [])
                }
                if deps <= {m.name for m in done}:
                    done.append(manifest)
                    progressed = True
                else:
                    remaining.append(manifest)
            pending = remaining
            if not progressed:
                break  # 成环：交给 validate() 报错
        return tuple(done)


def _load_one(ep: Any) -> Manifest:
    """加载一个入口点并校验其返回值。"""
    name = getattr(ep, "name", "<unknown>")
    try:
        factory = ep.load()
    except Exception as exc:  # 插件包自身 import 失败
        raise PluginError(f"外部插件 {name!r} 加载失败：{exc}") from exc
    if not callable(factory):
        raise PluginError(f"外部插件 {name!r} 的入口点必须是无参可调用对象")
    try:
        manifest = factory()
    except Exception as exc:  # 清单工厂自身抛错（含插件包内 import 失败）
        raise PluginError(f"外部插件 {name!r} 加载失败：{exc}") from exc
    if not isinstance(manifest, Manifest):
        raise PluginError(
            f"外部插件 {name!r} 返回了 {type(manifest)!r}，期望 core.plugins.manifest.Manifest"
        )
    if manifest.module is None:
        raise PluginError(f"外部插件 {name!r} 的清单缺少 module（包导入路径）")
    if manifest.name != name:
        raise PluginError(
            f"外部插件入口点名 {name!r} 与清单 name={manifest.name!r} 不一致"
        )
    return manifest


def discover_entry_points(group: str = ENTRY_POINT_GROUP) -> tuple[Manifest, ...]:
    """读取已安装发行版声明的外部插件清单；没有装任何插件时返回空元组。"""
    return tuple(_load_one(ep) for ep in entry_points(group=group))
