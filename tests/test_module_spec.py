"""模块清单（``app.modules.spec``）的等价性与结构约束。

这四张派生表拆分前是分别手写的字面量（``registry.MODULES``、``registry._ERROR_MODULES``、
``app.bootstrap.APP_MODEL_MODULES``、``app/bootstrap.APP_TASK_MODULES``）。派生规则一旦写错，
表现是**运行时 404 或缺错误码**——进程照常启动，所以这里把旧值逐项钉死。

步骤 9 切片 1 把问答拆成独立模块 ``qa``（目录名排在 ``projects`` 与 ``rbac`` 之间），
切片 2 把评论拆成 ``comment``（排在 ``admin`` 与 ``content`` 之间），其错误码/模型/任务随之
从 content 的清单里移出。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

from app.modules import registry, spec
from core.plugins.manifest import ManifestError

# ── 拆分前的字面量（顺序即语义：路由注册顺序 / 错误码导入顺序）─────────────
_LEGACY_MODULES = (
    "admin",
    "blog",
    "columns",
    "comment",
    "content",
    "exam",
    "feed",
    "files",
    "health",
    "interaction",
    "notification",
    "points",
    "projects",
    "qa",
    "search",
    "starhope",
    "treehole",
)

_LEGACY_ERROR_MODULES = (
    "app.modules.blog.errors",
    "app.modules.columns.errors",
    "app.modules.comment.errors",
    "app.modules.content.errors",
    "app.modules.exam.errors",
    "app.modules.files.errors",
    "app.modules.interaction.errors",
    "app.modules.notification.errors",
    "app.modules.points.errors",
    "app.modules.projects.errors",
    "app.modules.qa.errors",
    "app.modules.search.errors",
    "app.modules.starhope.errors",
    "app.modules.admin.moderation",
    "app.modules.content.articles",
    "app.modules.content.boards",
)

_LEGACY_MODEL_MODULES = (
    "app.modules.admin.models",
    # 步骤 9 切片 3：博客模型迁出 content（表名不变，无需迁移）
    "app.modules.blog.models",
    # 步骤 9 切片 2：评论模型迁出 content（表名不变，无需迁移）
    "app.modules.columns.models",
    "app.modules.comment.models",
    "app.modules.content.articles.models",
    "app.modules.content.models",
    "app.modules.exam.models",
    "app.modules.feed.models",
    "app.modules.files.models",
    "app.modules.interaction.models",
    "app.modules.notification.models",
    "app.modules.points.models",
    "app.modules.projects.models",
    # 步骤 9 切片 1：问答模型迁出 content（表名不变，无需迁移）
    "app.modules.qa.models",
    # 步骤 7：RolePermission 从 admin.models 迁入 rbac 域（表名不变，无需迁移）；
    # rbac 目录名排在 projects 与 search 之间，故此处按清单顺序插入。
    "app.modules.rbac.models",
    "app.modules.starhope.models",
    "app.modules.treehole.models",
)

_LEGACY_TASK_MODULES = (
    "app.modules.admin.tasks",
    "app.modules.blog.tasks",
    "app.modules.content.tasks",
    "app.modules.feed.tasks",
    "app.modules.files.tasks",
    "app.modules.interaction.tasks",
    "app.modules.notification.tasks",
    "app.modules.points.tasks",
    "app.modules.qa.tasks",
    "app.modules.search.tasks",
)


def test_modules_matches_legacy_literal() -> None:
    assert spec.MODULES == _LEGACY_MODULES


def test_error_modules_match_legacy_literal() -> None:
    assert spec.ERROR_MODULES == _LEGACY_ERROR_MODULES


def test_model_modules_match_legacy_literal() -> None:
    assert spec.APP_MODEL_MODULES == _LEGACY_MODEL_MODULES


def test_task_modules_match_legacy_literal() -> None:
    assert spec.APP_TASK_MODULES == _LEGACY_TASK_MODULES


def test_registry_reexports_the_same_tables() -> None:
    """registry 只是 spec 的门面：两边必须是同一对象，避免又长出第二份副本。"""
    assert registry.MODULES is spec.MODULES
    assert registry.ERROR_MODULES is spec.ERROR_MODULES


def test_rbac_is_loaded_but_not_aggregated() -> None:
    """rbac 承载跨模块权限原语，但没有 REST/GraphQL 出口，不应进 MODULES。"""
    names = {s.name for s in spec.SPECS}
    assert "rbac" in names
    assert "rbac" not in spec.MODULES


@pytest.mark.parametrize("name", [s.name for s in spec.SPECS])
def test_every_spec_has_importable_package(name: str) -> None:
    """清单里的模块必须真实存在——写错名字不该等到 404 才被发现。"""
    module = import_module(f"app.modules.{name}")
    assert module is not None


@pytest.mark.parametrize("name", spec.MODULES)
def test_aggregated_module_exposes_public_api(name: str) -> None:
    """聚合模块须在 __init__.py 暴露 ROUTERS 或 GRAPHQL（否则聚合到的是空集）。"""
    module = import_module(f"app.modules.{name}")
    assert hasattr(module, "ROUTERS") or hasattr(module, "GRAPHQL"), name


def test_generated_paths_are_unique() -> None:
    """重复路径意味着两处声明打架：模型/任务重复导入、错误码重复 register。"""
    assert len(set(spec.MODULES)) == len(spec.MODULES)
    assert len(set(spec.ERROR_MODULES)) == len(spec.ERROR_MODULES)
    assert len(set(spec.APP_MODEL_MODULES)) == len(spec.APP_MODEL_MODULES)
    assert len(set(spec.APP_TASK_MODULES)) == len(spec.APP_TASK_MODULES)


def test_every_module_package_has_a_manifest() -> None:
    """新增 ``app/modules/<x>/`` 却忘了写清单 → 该模块从此不被装配。

    这是清单化后新的失效模式（旧的失效模式是「忘记改 registry 元组」），必须挡在 CI。
    """
    root = Path(spec.__file__).parent
    packages = {
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }
    assert packages == {s.name for s in spec.SPECS}


def test_spec_name_must_match_directory(tmp_path: Path) -> None:
    """清单 name 与目录名不一致时装配名会对不上，须在加载期拒绝。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="wrong", source=tmp_path / "alpha" / "manifest.toml", aggregate=True
    )
    with pytest.raises(ManifestError, match="与目录名"):
        spec._to_spec(manifest, builtin=True)


def test_external_plugin_must_declare_module(tmp_path: Path) -> None:
    """外部插件没有 ``app.modules.<name>`` 默认路径可回退，缺 module 只能报错。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(name="demo", source=tmp_path / "demo" / "manifest.toml")
    with pytest.raises(ManifestError, match="必须声明 module"):
        spec._to_spec(manifest, builtin=False)


@pytest.mark.parametrize("module", ["app.modules.content", "core.plugins", "auth"])
def test_external_plugin_must_not_use_host_namespace(
    tmp_path: Path, module: str
) -> None:
    """第三方插件指向宿主包会让同一模块被二次装载（路由挂两次、错误码重复注册）。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="evil", source=tmp_path / "evil" / "manifest.toml", module=module
    )
    with pytest.raises(ManifestError, match="宿主命名空间"):
        spec._to_spec(manifest, builtin=False)


def test_external_plugin_resources_must_not_use_host_namespace(
    tmp_path: Path,
) -> None:
    """module 受保护但资源路径不受保护的话，插件仍可声明宿主模型造成二次装载。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="evil",
        source=tmp_path / "evil" / "manifest.toml",
        module="evilpkg.main",
        models=("core.db.base",),
    )
    with pytest.raises(ManifestError, match="宿主命名空间"):
        spec._to_spec(manifest, builtin=False)


def test_builtin_module_path_cannot_be_declared(tmp_path: Path) -> None:
    """内置模块的 module 由目录名推导：声明了只会让清单与目录静默不一致。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="alpha",
        source=tmp_path / "alpha" / "manifest.toml",
        module="app.modules.beta",
    )
    with pytest.raises(ManifestError, match="不得声明"):
        spec._to_spec(manifest, builtin=True)


def test_builtin_resources_must_stay_in_own_namespace(tmp_path: Path) -> None:
    """跨包声明资源会让两个模块的模型/任务/错误码互相串装，登记表上看不出归属。"""
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="alpha",
        source=tmp_path / "alpha" / "manifest.toml",
        models=("app.modules.beta.models",),
    )
    with pytest.raises(ManifestError, match="不在本模块命名空间"):
        spec._to_spec(manifest, builtin=True)


def test_duplicate_resource_across_manifests_is_rejected() -> None:
    """两个模块抢同一个模型/任务/错误码模块时，「谁负责」失去唯一答案。"""
    from app.modules.spec import ModuleSpec

    with pytest.raises(ManifestError, match="同时由"):
        spec._check_unique_resources(
            (
                ModuleSpec(name="alpha", module_path="alpha", models=("shared.models",)),
                ModuleSpec(name="beta", module_path="beta", models=("shared.models",)),
            )
        )


def test_duplicate_module_path_across_manifests_is_rejected() -> None:
    """两个名字指向同一个包 → 路由/模型会被装载两次。"""
    from app.modules.spec import ModuleSpec

    with pytest.raises(ManifestError, match="模块路径"):
        spec._check_unique_resources(
            (
                ModuleSpec(name="alpha", module_path="same.pkg"),
                ModuleSpec(name="beta", module_path="same.pkg"),
            )
        )


def test_graphql_versions_derivation(tmp_path: Path) -> None:
    """未声明版本的 aggregate 模块进 v1；声明了 v2 的模块只进 v2；非 aggregate 不参与。"""
    from app.modules.spec import ModuleSpec

    assert spec._derive_graphql_versions(
        (
            ModuleSpec(name="alpha", module_path="alpha"),
            ModuleSpec(name="beta", module_path="beta", graphql_versions=("v2",)),
            ModuleSpec(name="gamma", module_path="gamma", aggregate=False),
        )
    ) == {"v1": ("alpha",), "v2": ("beta",)}


def test_graphql_versions_on_non_aggregate_module_is_rejected(tmp_path: Path) -> None:
    from core.plugins.manifest import Manifest

    manifest = Manifest(
        name="alpha",
        source=tmp_path / "alpha" / "manifest.toml",
        aggregate=False,
        graphql_versions=("v2",),
    )
    with pytest.raises(ManifestError, match="aggregate=true"):
        spec._to_spec(manifest, builtin=True)


def test_resolve_migration_dir_accepts_package_only() -> None:
    """迁移路径必须是包目录；普通模块或不可导入路径在装配期报错。"""
    resolved = spec._resolve_migration_dir("core.plugins")
    assert resolved.name == "plugins"
    with pytest.raises(ManifestError, match="无法导入"):
        spec._resolve_migration_dir("no_such_package_xyz")
    with pytest.raises(ManifestError, match="不是包"):
        spec._resolve_migration_dir("core.plugins.manifest")


def test_plugin_denylist_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "plugin_denylist", " alpha, beta ,, ")
    assert spec._disabled_plugins() == frozenset({"alpha", "beta"})


def test_assemble_imports_every_plugin_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 aggregate 插件也必须在装配期被 import（它唯一的初始化钩子）。"""
    import importlib
    from types import SimpleNamespace

    from boot import assemble as boot_assemble

    calls: list[str] = []
    monkeypatch.setattr(
        spec,
        "SPECS",
        (
            SimpleNamespace(name="one", module_path="fake.plugin.one"),
            SimpleNamespace(name="two", module_path="fake.plugin.two"),
        ),
    )
    monkeypatch.setattr(importlib, "import_module", calls.append)
    boot_assemble._import_plugins()
    assert calls == ["fake.plugin.one", "fake.plugin.two"]


def test_assemble_wraps_plugin_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    from types import SimpleNamespace

    from boot import assemble as boot_assemble

    monkeypatch.setattr(
        spec,
        "SPECS",
        (SimpleNamespace(name="broken", module_path="fake.broken"),),
    )

    def _boom(path: str) -> None:
        raise ImportError("nope")

    monkeypatch.setattr(importlib, "import_module", _boom)
    with pytest.raises(RuntimeError, match=r"插件 'broken'.*加载失败"):
        boot_assemble._import_plugins()


def test_registry_plugin_config_requires_declared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置键必须先在清单声明：拼错键名立刻报错，而不是静默读到 None。"""
    import os
    from types import SimpleNamespace

    monkeypatch.setattr(
        registry, "SPECS", (SimpleNamespace(name="demo", config=("api_key",)),)
    )
    monkeypatch.setenv("LKM_PLUGIN_DEMO_API_KEY", "secret")
    assert registry.plugin_config("demo", "api_key") == "secret"
    assert registry.plugin_config("demo", "api_key", "fallback") == "secret"
    with pytest.raises(ValueError, match="未在 manifest 声明"):
        registry.plugin_config("demo", "typo_key")
    with pytest.raises(ValueError, match="未登记"):
        registry.plugin_config("nobody", "api_key")
    monkeypatch.delenv("LKM_PLUGIN_DEMO_API_KEY")
    assert registry.plugin_config("demo", "api_key") is None
    assert os.environ.get("LKM_PLUGIN_DEMO_API_KEY") is None


def test_registry_middlewares_collects_from_all_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    class _First:
        pass

    class _Second:
        pass

    first = ModuleType("fake_mw_a")
    first.MIDDLEWARE = [_First]
    second = ModuleType("fake_mw_b")
    second.MIDDLEWARE = [_Second]
    monkeypatch.setitem(sys.modules, "fake_mw_a", first)
    monkeypatch.setitem(sys.modules, "fake_mw_b", second)
    monkeypatch.setattr(
        registry,
        "SPECS",
        (
            SimpleNamespace(name="a"),
            SimpleNamespace(name="b"),
        ),
    )
    monkeypatch.setattr(
        registry, "MODULE_PATHS", {"a": "fake_mw_a", "b": "fake_mw_b"}
    )
    assert registry.middlewares() == [_First, _Second]
