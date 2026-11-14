"""
业务模块清单：模块注册的**单一事实源**（蓝图 §7）。

事实源是各模块自己的 ``manifest.toml``：内置模块在 ``app/modules/<name>/manifest.toml``，
外部插件由已安装发行版通过入口点 ``lkm.plugins`` 提供。本模块只做四件事：收集清单 →
交给 :class:`~core.plugins.host.PluginSet` 校验定序 → 校验资源路径归属与跨清单重复 →
派生各张注册表（路由、错误码、模型、任务、GraphQL 版本、迁移目录、配置键）。

新增一个业务域 = 新建目录 + 写一份 manifest，**不再改** ``registry.py`` 或
``app/bootstrap.py``。拆分前这四项是分散在两个文件里的手写字面量，改漏一处的表现是
运行时 404 或缺错误码，而不是启动失败——所以 ``tests/test_module_spec.py`` 把旧值逐项钉死。

派生规则（刻意与拆分前的导入顺序逐项一致）：

- ``MODULES``：``aggregate`` 模块，按清单顺序——顺序决定路由注册顺序；
- ``ERROR_MODULES``：先各模块的顶层 ``<module>.errors``，再其声明的子包错误码模块；
- ``APP_MODEL_MODULES`` / ``APP_TASK_MODULES``：按清单顺序展平。

auth 不在本表内：它已独立成顶层包，模型/错误码/任务由 ``auth.bootstrap`` 自行注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from core.plugins.host import PluginSet, discover_entry_points
from core.plugins.manifest import Manifest, ManifestError, load_manifests

_MODULES_DIR = Path(__file__).parent

#: 宿主自己的顶层包：外部插件的 ``module`` 不得落在这些命名空间内。
#: 冒用会让宿主模块被二次装载（同一 ``ROUTERS`` 挂两次、错误码重复注册），而
#: import-linter 合同根本看不到外部发行版，等于绕过全部边界校验。
_HOST_PACKAGES = frozenset({"app", "auth", "boot", "core"})


@dataclass(frozen=True)
class ModuleSpec:
    """一个业务域在装配期的全部注册项（由 manifest.toml 翻译而来）。"""

    name: str
    #: 包导入路径。内置模块为 ``app.modules.<name>``；外部插件由清单的 ``module`` 声明。
    module_path: str
    #: 错误码模块的完整路径（导入即 ``register()`` 副作用）；含顶层与子包两类。
    errors: tuple[str, ...] = ()
    #: ORM 模型模块（全量 metadata → relationship 字符串解析）。
    models: tuple[str, ...] = ()
    #: Pulsar 任务模块（导入即副作用注册 handler / cron）。
    tasks: tuple[str, ...] = ()
    #: 是否参与 REST/GraphQL 聚合。rbac 只提供跨模块权限原语，不导出路由/字段。
    aggregate: bool = True
    #: 插件自带 Alembic 迁移目录的包路径（空 = 只走宿主迁移链）。
    migrations: str | None = None
    #: 参与的 GraphQL 版本；空 = ``("v1",)``。仅 aggregate 模块生效。
    graphql_versions: tuple[str, ...] = ()
    #: 插件声明的配置键（``registry.plugin_config`` 只接受这里出现的键）。
    config: tuple[str, ...] = ()

    @property
    def top_errors_module(self) -> str:
        """该模块的顶层错误码模块路径（是否存在由 ``errors`` 决定）。"""
        return f"{self.module_path}.errors"


#: 清单中声明「导入路径」的字段（装配期按类别分别登记）。
_RESOURCE_FIELDS = ("errors", "models", "tasks")


def _check_builtin_namespace(manifest: Manifest, prefix: str) -> None:
    """内置模块声明的资源路径必须落在自己的包下。

    写成 ``app.modules.points.models`` 这类跨包路径时，装配期仍会正常导入——两个模块
    的模型/任务/错误码因此互相串装，而登记表上看不出是谁声明的。这里直接拒绝，让错误
    在清单加载期带模块名报出。
    """
    for kind in _RESOURCE_FIELDS:
        for path in getattr(manifest, kind):
            if path != prefix and not path.startswith(f"{prefix}."):
                raise ManifestError(
                    f"{manifest.source}: {kind} 路径 {path!r} 不在本模块命名空间"
                    f" {prefix!r} 下"
                )


def _check_external_namespace(manifest: Manifest) -> None:
    """外部插件声明的资源路径同样不得落进宿主包。

    ``module`` 本身的宿主命名空间检查在下面；资源路径若不受同样约束，插件仍可声明
    ``models = ["app.modules.points.models"]`` 造成宿主模块二次装载——等于绕开保护。
    """
    for kind in _RESOURCE_FIELDS:
        for path in getattr(manifest, kind):
            top = path.split(".", 1)[0]
            if top in _HOST_PACKAGES:
                raise ManifestError(
                    f"{manifest.name}: 外部插件的 {kind} 路径 {path!r} 落在宿主命名空间"
                    f"（{'/'.join(sorted(_HOST_PACKAGES))}）内：会二次装载宿主模块"
                )


def _to_spec(manifest: Manifest, *, builtin: bool) -> ModuleSpec:
    """清单 → 规格。

    内置模块额外要求「目录名 == name」：清单写错名字时，装配出来的模块名与目录对不上，
    只会在访问路由时 404。外部插件的目录名由发行版决定，不做该约束，但必须显式声明
    ``module`` 且不得占用宿主命名空间。

    内置模块的 ``module`` 由目录名推导，声明了也不会生效（反而与目录不一致时静默走错
    路径），因此直接拒绝。
    """
    if manifest.graphql_versions and not manifest.aggregate:
        raise ManifestError(
            f"{manifest.source}: graphql_versions 仅对 aggregate=true 的模块生效"
        )
    if builtin:
        directory = manifest.source.parent.name
        if manifest.name != directory:
            raise ManifestError(
                f"{manifest.source}: name={manifest.name!r} 与目录名 {directory!r} 不一致"
            )
        if manifest.module is not None:
            raise ManifestError(
                f"{manifest.source}: 内置模块的 module 由目录名推导，不得声明"
            )
        _check_builtin_namespace(manifest, f"app.modules.{manifest.name}")
    else:
        if manifest.module is None:
            raise ManifestError(
                f"{manifest.name}: 外部插件必须声明 module（包导入路径）"
            )
        top = manifest.module.split(".", 1)[0]
        if top in _HOST_PACKAGES:
            raise ManifestError(
                f"{manifest.name}: 外部插件的 module={manifest.module!r} 落在宿主命名空间"
                f"（{'/'.join(sorted(_HOST_PACKAGES))}）内：会二次装载宿主模块"
            )
        _check_external_namespace(manifest)
    return ModuleSpec(
        name=manifest.name,
        module_path=manifest.module or f"app.modules.{manifest.name}",
        errors=manifest.errors,
        models=manifest.models,
        tasks=manifest.tasks,
        aggregate=manifest.aggregate,
        migrations=manifest.migrations,
        graphql_versions=manifest.graphql_versions,
        config=manifest.config,
    )


def _check_unique_resources(specs: tuple[ModuleSpec, ...]) -> None:
    """同一模块路径 / 同一类资源路径只能由一个模块声明。

    重复声明的实际后果是「谁负责这个模型/任务/错误码」失去唯一答案：重复 import 被
    Python 缓存掩盖，看起来一切正常，直到某天两个模块开始争同一个 handler 覆盖注册。
    跨内置/外部来源也同样适用。
    """
    owners: dict[str, str] = {}
    for spec_ in specs:
        owner = owners.setdefault(f"module:{spec_.module_path}", spec_.name)
        if owner != spec_.name:
            raise ManifestError(
                f"模块路径 {spec_.module_path!r} 同时被 {owner!r} 与 {spec_.name!r} 声明"
            )
        for kind in _RESOURCE_FIELDS:
            for path in getattr(spec_, kind):
                owner = owners.setdefault(f"{kind}:{path}", spec_.name)
                if owner != spec_.name:
                    raise ManifestError(
                        f"{kind} 路径 {path!r} 同时由 {owner!r} 与 {spec_.name!r} 声明"
                    )


def _disabled_plugins() -> frozenset[str]:
    """``LKM_PLUGIN_DENYLIST``：逗号分隔的外部插件名，在入口点加载前剔除。"""
    from core.config import settings

    return frozenset(
        name.strip() for name in settings.plugin_denylist.split(",") if name.strip()
    )


def _load_specs() -> tuple[ModuleSpec, ...]:
    """收集内置 + 外部清单，校验（重名/能力唯一/requires/成环/资源归属）后按依赖定序。"""
    builtin = load_manifests(_MODULES_DIR)
    builtin_names = {m.name for m in builtin}
    disabled = _disabled_plugins()
    blocked = sorted(disabled & builtin_names)
    if blocked:
        raise ManifestError(
            f"LKM_PLUGIN_DENYLIST 不能禁用内置模块：{blocked}"
            "（内置模块是宿主的一部分，只能禁用外部插件）"
        )
    ordered = PluginSet(
        (*builtin, *discover_entry_points(exclude=disabled))
    ).validate()
    specs = tuple(
        _to_spec(manifest, builtin=manifest.name in builtin_names)
        for manifest in ordered
    )
    _check_unique_resources(specs)
    return specs


def _resolve_migration_dir(package: str) -> Path:
    """把插件声明的迁移包路径解析成目录（目录下直接放 revision 文件）。"""
    found = find_spec(package)
    if found is None:
        raise ManifestError(f"迁移目录包 {package!r} 无法导入")
    locations = found.submodule_search_locations
    if not locations:
        raise ManifestError(f"迁移目录包 {package!r} 不是包（需是含 revision 文件的目录）")
    return Path(next(iter(locations)))


def _derive_graphql_versions(
    specs: tuple[ModuleSpec, ...],
) -> dict[str, tuple[str, ...]]:
    """版本 → 参与该版本的模块名（按清单顺序）。未声明版本的 aggregate 模块进 ``v1``。"""
    versions: dict[str, list[str]] = {}
    for spec_ in specs:
        if not spec_.aggregate:
            continue
        for version in spec_.graphql_versions or ("v1",):
            names = versions.setdefault(version, [])
            if spec_.name not in names:
                names.append(spec_.name)
    return {version: tuple(names) for version, names in versions.items()}


SPECS: tuple[ModuleSpec, ...] = _load_specs()

#: 插件自带迁移目录（按清单顺序）；``alembic/env.py`` 追加进 ``version_locations``。
MIGRATION_DIRS: tuple[Path, ...] = tuple(
    _resolve_migration_dir(s.migrations) for s in SPECS if s.migrations
)

#: 模块名 → 包导入路径（外部插件的路径与 ``app.modules.<name>`` 不同）。
MODULE_PATHS: dict[str, str] = {s.name: s.module_path for s in SPECS}

#: 参与 REST/GraphQL 聚合的模块名，按清单顺序（顺序即路由注册顺序）。
MODULES: tuple[str, ...] = tuple(s.name for s in SPECS if s.aggregate)

#: GraphQL 版本 → 参与模块；未声明 ``graphql_versions`` 的 aggregate 模块默认进 ``v1``。
GRAPHQL_VERSIONS: dict[str, tuple[str, ...]] = _derive_graphql_versions(SPECS)

#: 需在装配期导入以触发 ``register()`` 的错误码模块，按清单顺序。
#: 先顶层再子包，保持拆分前的导入顺序（重复码由 ``core.err.register`` 拒收，顺序不影响判定，
#: 但保持稳定可让「和旧值逐项相等」的等价性测试有意义）。
#: **与 aggregate 无关**：不导出路由的模块（如 rbac）同样可以有自己的错误码。
ERROR_MODULES: tuple[str, ...] = (
    *(s.top_errors_module for s in SPECS if s.top_errors_module in s.errors),
    *(e for s in SPECS for e in s.errors if e != s.top_errors_module),
)

#: 需在装配期导入的 ORM 模型模块（全量 metadata）。
APP_MODEL_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.models)

#: 需在装配期导入的 Pulsar 任务模块（导入即副作用注册）。
APP_TASK_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.tasks)
