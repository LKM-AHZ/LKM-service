"""模块清单（``app.modules.spec``）的等价性与结构约束。

这四张派生表拆分前是分别手写的字面量（``registry.MODULES``、``registry._ERROR_MODULES``、
``app.bootstrap.APP_MODEL_MODULES``、``app/bootstrap.APP_TASK_MODULES``）。派生规则一旦写错，
表现是**运行时 404 或缺错误码**——进程照常启动，所以这里把旧值逐项钉死。

步骤 9 切片 1 把问答拆成独立模块 ``qa``，故新增一条：``qa`` 目录名排在 ``projects`` 与
``rbac`` 之间，其错误码/模型/任务随之从 content 的清单里移出。
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
    "app.modules.content.blog",
    "app.modules.content.boards",
    "app.modules.content.columns",
)

_LEGACY_MODEL_MODULES = (
    "app.modules.admin.models",
    "app.modules.content.articles.models",
    "app.modules.content.blog.models",
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
    "app.modules.content.blog.tasks",
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
