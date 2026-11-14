"""
清单（``manifest.toml``）的通用加载器。

只做「文件 → 结构化对象 + 形状校验」，不解释字段含义。三条刻意选择的严格规则：

1. **未知字段即报错**：拼错的键（如 ``model`` vs ``models``）若被静默忽略，表现是模块
   没被加载、接口 404——启动完全正常，正是最难查的那类故障。同理，清单里**不声明没有
   读取方的字段**：早期的 ``routers`` / ``graphql`` 只被解析、无人消费（REST/GraphQL 出口
   实际由模块 ``__init__.py`` 的 ``ROUTERS`` / ``GRAPHQL`` 约定提供），第三方插件照着写
   只会拿到 0 条路由与一串 404。字段删除后，写它会命中本条第 1 款；
2. **缺失 name / 空字符串 / 重复项 / 非法类型即报错**：空路径会让装配期抛难以定位的
   空模块名，重复项则被下游注册表静默去重（「写了两次」看起来生效了）。两条路径
   ——清单文件与外部插件工厂——共用 :meth:`Manifest.__post_init__` 的同一套形状校验；
3. **按目录名排序返回**：目录扫描顺序在各文件系统上不保证一致，而调用方可能依赖顺序
   （如路由注册顺序）。排序让结果确定；顺序本身的语义由调用方的等价性测试钉住。
"""

from __future__ import annotations

import re
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
        "migrations",
        "graphql_versions",
        "config",
    }
)

#: 形状校验覆盖的列表字段（文件加载与外部插件工厂两条构造路径共用）。
_LIST_FIELDS = (
    "errors",
    "models",
    "tasks",
    "provides",
    "requires",
    "graphql_versions",
    "config",
)

#: GraphQL 版本名即端点路径段（``/graphql/<version>``），只允许 ``v<数字>``。
_GRAPHQL_VERSION_RE = re.compile(r"v\d+")


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
    #: 插件自带 Alembic 迁移目录的包路径（该包目录下直接放 revision 文件）。
    migrations: str | None = None
    #: 参与哪些 GraphQL 版本；空 = 默认 ``v1``。仅 aggregate 模块生效。
    graphql_versions: tuple[str, ...] = ()
    #: 插件声明的配置键（读取走 ``app.modules.registry.plugin_config``，未声明的键直报错）。
    config: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """守住两条构造路径（清单文件、外部插件工厂）共有的形状不变量。

        外部插件的清单由入口点工厂直接返回 :class:`Manifest`，不经过
        :func:`load_manifest`——若只在那里校验，工厂返回的清单可以带空路径或重复项，
        最终表现为「模块静默没装上」。
        """
        if not isinstance(self.name, str) or not self.name:
            raise ManifestError("name 必须是非空字符串")
        if self.module is not None and (
            not isinstance(self.module, str) or not self.module
        ):
            raise ManifestError("module 必须是非空字符串或省略")
        if not isinstance(self.aggregate, bool):
            raise ManifestError("aggregate 必须是布尔值")
        if self.migrations is not None and (
            not isinstance(self.migrations, str) or not self.migrations
        ):
            raise ManifestError("migrations 必须是非空字符串或省略")
        for version in self.graphql_versions:
            if not _GRAPHQL_VERSION_RE.fullmatch(version):
                raise ManifestError(
                    f"GraphQL 版本名 {version!r} 非法：只允许 v<数字>"
                )
        for key in _LIST_FIELDS:
            values = getattr(self, key)
            if any(not isinstance(v, str) or not v for v in values):
                raise ManifestError(f"字段 {key!r} 必须是非空字符串数组")
            if len(set(values)) != len(values):
                raise ManifestError(f"字段 {key!r} 存在重复项")


def _str_tuple(data: dict[str, Any], key: str, *, where: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v for v in value
    ):
        raise ManifestError(f"{where}: 字段 {key!r} 必须是非空字符串数组")
    if len(set(value)) != len(value):
        raise ManifestError(f"{where}: 字段 {key!r} 存在重复项")
    return tuple(value)


def _opt_str(data: dict[str, Any], key: str, *, where: Path) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ManifestError(f"{where}: 字段 {key!r} 必须是非空字符串")
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
        migrations=_opt_str(raw, "migrations", where=path),
        graphql_versions=_str_tuple(raw, "graphql_versions", where=path),
        config=_str_tuple(raw, "config", where=path),
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
