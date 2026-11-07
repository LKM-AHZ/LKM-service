"""
清单（``manifest.toml``）的通用加载器。

只做「文件 → 结构化对象 + 形状校验」，不解释字段含义。三条刻意选择的严格规则：

1. **未知字段即报错**：拼错的键（如 ``model`` vs ``models``）若被静默忽略，表现是模块
   没被加载、接口 404——启动完全正常，正是最难查的那类故障。同理，清单里**不声明没有
   读取方的字段**：早期的 ``routers`` / ``graphql`` 只被解析、无人消费（REST/GraphQL 出口
   实际由模块 ``__init__.py`` 的 ``ROUTERS`` / ``GRAPHQL`` 约定提供），第三方插件照着写
   只会拿到 0 条路由与一串 404。字段删除后，写它会命中本条第 1 款；
2. **缺失 name / 非法类型即报错**：同上，fail-fast 优于降级；
3. **按目录名排序返回**：目录扫描顺序在各文件系统上不保证一致，而调用方可能依赖顺序
   （如路由注册顺序）。排序让结果确定；顺序本身的语义由调用方的等价性测试钉住。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.toml"

#: 允许出现的字段。新增字段须同时更新这里，避免「写了但没人读」。
#: 这里**没有** ``routers`` / ``graphql``：那两个字段曾被解析但没有任何读取方。
_KNOWN_FIELDS = frozenset(
    {
        "name",
        "module",
        "errors",
        "models",
        "tasks",
        "provides",
        "requires",
        "aggregate",
    }
)


class ManifestError(ValueError):
    """清单缺失或字段非法：装配期 fail-fast，绝不静默跳过某个模块。"""


@dataclass(frozen=True)
class Manifest:
    """一份插件清单的解析结果（字段含义由调用方解释）。"""

    name: str
    source: Path
    #: 包导入路径。内置模块留空（默认 ``app.modules.<name>``）；外部插件必须显式声明。
    module: str | None = None
    errors: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    aggregate: bool = True


def _str_tuple(data: dict[str, Any], key: str, *, where: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ManifestError(f"{where}: 字段 {key!r} 必须是字符串数组")
    return tuple(value)


def _opt_str(data: dict[str, Any], key: str, *, where: Path) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ManifestError(f"{where}: 字段 {key!r} 必须是字符串")
    return value


def load_manifest(path: Path) -> Manifest:
    """解析单个清单文件；任何形状问题都抛 :class:`ManifestError`。"""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: TOML 解析失败：{exc}") from exc

    unknown = set(raw) - _KNOWN_FIELDS
    if unknown:
        raise ManifestError(f"{path}: 未知字段 {sorted(unknown)}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"{path}: 缺少非空 name")

    aggregate = raw.get("aggregate", True)
    if not isinstance(aggregate, bool):
        raise ManifestError(f"{path}: 字段 'aggregate' 必须是布尔值")

    return Manifest(
        name=name,
        source=path,
        module=_opt_str(raw, "module", where=path),
        errors=_str_tuple(raw, "errors", where=path),
        models=_str_tuple(raw, "models", where=path),
        tasks=_str_tuple(raw, "tasks", where=path),
        provides=_str_tuple(raw, "provides", where=path),
        requires=_str_tuple(raw, "requires", where=path),
        aggregate=aggregate,
    )


def load_manifests(directory: Path) -> tuple[Manifest, ...]:
    """扫描 ``directory/*/manifest.toml`` 并按目录名排序返回。

    只扫描**有清单的子目录**：没有清单的目录（``__pycache__``、未来非插件目录）被跳过，
    这是唯一允许的静默；缺清单的业务模块会被调用方的等价性测试发现。
    """
    if not directory.is_dir():
        raise ManifestError(f"清单目录不存在：{directory}")

    manifests = [
        load_manifest(child / MANIFEST_FILENAME)
        for child in sorted(directory.iterdir(), key=lambda p: p.name)
        if child.is_dir() and (child / MANIFEST_FILENAME).is_file()
    ]
    if not manifests:
        raise ManifestError(f"{directory} 下没有任何 {MANIFEST_FILENAME}")
    return tuple(manifests)
