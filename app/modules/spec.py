"""
业务模块清单：模块注册的**单一事实源**（蓝图 §7）。

事实源现在是各模块自己的 ``manifest.toml``（``app/modules/<name>/manifest.toml``），
本模块只做两件事：把清单翻译成 :class:`ModuleSpec`，再派生四张注册表。

新增一个业务域 = 新建目录 + 写一份 manifest，**不再改** ``registry.py`` 或
``app/bootstrap.py``。拆分前这四项是分散在两个文件里的手写字面量，改漏一处的表现是
运行时 404 或缺错误码，而不是启动失败——所以 ``tests/test_module_spec.py`` 把旧值逐项钉死。

派生规则（刻意与拆分前的导入顺序逐项一致）：

- ``MODULES``：``aggregate`` 模块，按清单顺序（目录名排序）——顺序决定路由注册顺序；
- ``ERROR_MODULES``：先各模块的顶层 ``app.modules.<name>.errors``，再其声明的子包错误码模块；
- ``APP_MODEL_MODULES`` / ``APP_TASK_MODULES``：按清单顺序展平。

auth 不在本表内：它已独立成顶层包，模型/错误码/任务由 ``auth.bootstrap`` 自行注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.plugins.manifest import Manifest, ManifestError, load_manifests

_MODULES_DIR = Path(__file__).parent


@dataclass(frozen=True)
class ModuleSpec:
    """一个业务域在装配期的全部注册项（由 manifest.toml 翻译而来）。"""

    name: str
    #: 错误码模块的完整路径（导入即 ``register()`` 副作用）；含顶层与子包两类。
    errors: tuple[str, ...] = ()
    #: ORM 模型模块（全量 metadata → relationship 字符串解析）。
    models: tuple[str, ...] = ()
    #: Pulsar 任务模块（导入即副作用注册 handler / cron）。
    tasks: tuple[str, ...] = ()
    #: 是否参与 REST/GraphQL 聚合。rbac 只提供跨模块权限原语，不导出路由/字段。
    aggregate: bool = True

    @property
    def top_errors_module(self) -> str:
        """该模块的顶层错误码模块路径（不存在时也返回路径，由 ``errors`` 决定是否导入）。"""
        return f"app.modules.{self.name}.errors"


def _to_spec(manifest: Manifest) -> ModuleSpec:
    """清单 → 规格。目录名与 ``name`` 必须一致，否则装配出来的模块名与目录对不上。"""
    directory = manifest.source.parent.name
    if manifest.name != directory:
        raise ManifestError(
            f"{manifest.source}: name={manifest.name!r} 与目录名 {directory!r} 不一致"
        )
    return ModuleSpec(
        name=manifest.name,
        errors=manifest.errors,
        models=manifest.models,
        tasks=manifest.tasks,
        aggregate=manifest.aggregate,
    )


SPECS: tuple[ModuleSpec, ...] = tuple(_to_spec(m) for m in load_manifests(_MODULES_DIR))

#: 参与 REST/GraphQL 聚合的模块名，按清单顺序（顺序即路由注册顺序）。
MODULES: tuple[str, ...] = tuple(s.name for s in SPECS if s.aggregate)

#: 需在装配期导入以触发 ``register()`` 的错误码模块，按清单顺序。
#: 先顶层再子包，保持拆分前的导入顺序（重复码由 ``core.err.register`` 拒收，顺序不影响判定，
#: 但保持稳定可让「和旧值逐项相等」的等价性测试有意义）。
ERROR_MODULES: tuple[str, ...] = (
    *(
        s.top_errors_module
        for s in SPECS
        if s.aggregate and s.top_errors_module in s.errors
    ),
    *(e for s in SPECS if s.aggregate for e in s.errors if e != s.top_errors_module),
)

#: 需在装配期导入的 ORM 模型模块（全量 metadata）。
APP_MODEL_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.models)

#: 需在装配期导入的 Pulsar 任务模块（导入即副作用注册）。
APP_TASK_MODULES: tuple[str, ...] = tuple(m for s in SPECS for m in s.tasks)
