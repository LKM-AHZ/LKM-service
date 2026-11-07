"""
插件宿主：发现 → 校验 → 定序。

本模块**不认识业务概念**，只处理 :class:`~core.plugins.manifest.Manifest` 集合：

- :class:`PluginSet`：重名 / 能力归属唯一 / ``requires`` 闭包校验、拓扑定序（依赖先装）；
- :func:`discover_entry_points`：从已安装发行版的 ``lkm.plugins`` 入口点读取外部插件。

内置业务模块与外部插件走同一套清单，差别只在清单来源：内置来自
``app/modules/*/manifest.toml``，外部来自 ``entry_points(group="lkm.plugins")``。

**为什么校验必须 fail-fast**：``requires`` 写错或成环时，若只是「少装了某模块」，表现是
运行时缺命令/缺字段（404 或空字段），而不是启动失败——这类故障最难定位。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from importlib.util import find_spec
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

    def validate(self) -> tuple[Manifest, ...]:
        """重名 / 能力归属唯一 / requires 闭包 / 成环，任一不满足即抛 :class:`PluginError`。

        返回值就是安装顺序（被依赖者在前）。让校验顺手返回定序，而不是让调用方再调一次
        :meth:`install_order`：两处都做一遍拓扑排序没有收益，漏调第二处还会让顺序悄悄改变。
        """
        names = [m.name for m in self.manifests]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise PluginError(f"插件重名：{duplicates}")

        providers = self._providers_by_capability()
        self._check_unique_capabilities(providers)
        self._check_requirements(providers)
        return self.install_order()

    def install_order(self) -> tuple[Manifest, ...]:
        """按 ``requires`` 拓扑排序（被依赖者在前）；无依赖时保持输入顺序。

        实现是稳定的 Kahn 算法：每轮按输入顺序取「依赖已满足」的节点，因此内置模块
        （``requires`` 为空）的顺序与清单顺序完全一致——路由注册顺序不会被动改变。

        成环时抛 :class:`PluginError`，**不**返回排序到一半的短列表：那个短列表会让部分
        模块静默消失（正是本模块 docstring 要消灭的 404 类故障）。
        """
        providers = self._providers_by_capability()
        pending = list(self.manifests)
        done: list[Manifest] = []
        done_names: set[str] = set()
        while pending:
            remaining: list[Manifest] = []
            for manifest in pending:
                deps = {
                    name for cap in manifest.requires for name in providers.get(cap, [])
                }
                if deps <= done_names:
                    done.append(manifest)
                    done_names.add(manifest.name)
                else:
                    remaining.append(manifest)
            if len(remaining) == len(pending):
                raise PluginError(f"插件依赖成环：{sorted(m.name for m in remaining)}")
            pending = remaining
        return tuple(done)

    def _providers_by_capability(self) -> dict[str, list[str]]:
        """能力名 → 提供方（按清单顺序）。``requires`` 只按能力名寻址，故必须唯一。"""
        providers: dict[str, list[str]] = {}
        for manifest in self.manifests:
            for cap in manifest.provides:
                providers.setdefault(cap, []).append(manifest.name)
        return providers

    @staticmethod
    def _check_unique_capabilities(providers: dict[str, list[str]]) -> None:
        """一个能力只能有一个提供方——这是「按名字寻址」的前提。

        允许多个提供方的话，消费方到底拿到哪份实现取决于装配顺序——同一类故障在不同
        插件组合下结论不同，因此这里直接拒收，而不是让 :meth:`install_order` 把所有
        提供方都当成前置依赖（那会造出「假环」）。
        """
        ambiguous = {cap: names for cap, names in providers.items() if len(names) > 1}
        if ambiguous:
            raise PluginError(
                f"能力被重复提供：{ambiguous}（requires 按能力名寻址，"
                "多个提供方等于「谁生效取决于装配顺序」）"
            )

    def _check_requirements(self, providers: dict[str, list[str]]) -> None:
        missing = {
            cap for m in self.manifests for cap in m.requires if cap not in providers
        }
        if missing:
            raise PluginError(
                f"requires 无法满足：{sorted(missing)}"
                f"（已提供的 provides：{sorted(providers)}）"
            )


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
    # module 写错时若只等装配期 import，报错是裸 ModuleNotFoundError，看不出是哪个插件。
    try:
        found = find_spec(manifest.module)
    except Exception as exc:  # 父包 import 期抛错也会从这里出来
        raise PluginError(
            f"外部插件 {name!r} 的 module={manifest.module!r} 无法解析：{exc}"
        ) from exc
    if found is None:
        raise PluginError(
            f"外部插件 {name!r} 的 module={manifest.module!r} 无法导入"
            "（发行版需把该模块装进当前解释器的 sys.path）"
        )
    return manifest


def discover_entry_points(group: str = ENTRY_POINT_GROUP) -> tuple[Manifest, ...]:
    """读取已安装发行版声明的外部插件清单，按入口点名排序；未安装插件时返回空元组。

    排序是刻意的：``importlib.metadata`` 的枚举顺序不保证跨环境一致，而清单顺序会决定
    装配顺序（理由同 :func:`~core.plugins.manifest.load_manifests` 按目录名排序）。
    """
    return tuple(
        _load_one(ep)
        for ep in sorted(
            entry_points(group=group), key=lambda e: getattr(e, "name", "")
        )
    )
