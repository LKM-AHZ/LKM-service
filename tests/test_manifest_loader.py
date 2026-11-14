"""``core.plugins.manifest`` 加载器的形状校验。

清单是唯一事实源，所以「拼错字段被静默忽略」是最危险的失效模式：进程照常启动，
只是某个模块的模型/任务没注册。这里把三条严格规则与排序行为逐条钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.plugins.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    load_manifests,
)


def _write(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / MANIFEST_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha", 'name = "alpha"\nmodel = ["a.models"]\n')
    with pytest.raises(ManifestError, match="未知字段"):
        load_manifest(path)


def test_route_fields_are_rejected_as_unknown(tmp_path: Path) -> None:
    """``routers`` / ``graphql`` 从未有读取方（出口走模块 ROUTERS/GRAPHQL 约定）。

    保留字段等于接受「写了但没生效」：插件声明了路由却拿到 0 条、只表现为 404。
    删掉字段后必须报「未知字段」，这才是可以被发现的失败。
    """
    for field, body in (
        ("routers", 'routers = "alpha.router:router"\n'),
        ("graphql", 'graphql = "alpha.graphql:Query"\n'),
    ):
        directory = tmp_path / field
        directory.mkdir(parents=True)
        path = directory / MANIFEST_FILENAME
        path.write_text(f'name = "{field}"\n{body}', encoding="utf-8")
        with pytest.raises(ManifestError, match=f"未知字段.*{field}"):
            load_manifest(path)


def test_missing_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha", 'models = ["a.models"]\n')
    with pytest.raises(ManifestError, match="缺少非空 name"):
        load_manifest(path)


def test_non_string_list_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha", 'name = "alpha"\nmodels = [1]\n')
    with pytest.raises(ManifestError, match="字符串数组"):
        load_manifest(path)


def test_empty_string_entry_is_rejected(tmp_path: Path) -> None:
    """空路径不是「没写」，装配期 import 会抛难以定位的空模块名。"""
    path = _write(tmp_path, "alpha", 'name = "alpha"\nmodels = [""]\n')
    with pytest.raises(ManifestError, match="非空字符串数组"):
        load_manifest(path)


def test_duplicate_entry_is_rejected(tmp_path: Path) -> None:
    """重复项会被下游注册表静默去重，让「写了两次」看起来生效了。"""
    path = _write(
        tmp_path, "alpha", 'name = "alpha"\nmodels = ["a.models", "a.models"]\n'
    )
    with pytest.raises(ManifestError, match="重复项"):
        load_manifest(path)


def test_empty_module_is_rejected(tmp_path: Path) -> None:
    """``module = ""`` 曾会被当成「未设置」而静默回退默认路径。"""
    path = _write(tmp_path, "alpha", 'name = "alpha"\nmodule = ""\n')
    with pytest.raises(ManifestError, match="非空字符串"):
        load_manifest(path)


def test_factory_constructed_manifest_is_validated(tmp_path: Path) -> None:
    """外部插件工厂直接构造 Manifest，不经过文件加载——形状校验必须同源生效。"""
    with pytest.raises(ManifestError, match="非空字符串数组"):
        Manifest(name="alpha", source=tmp_path / "manifest.toml", models=("",))
    with pytest.raises(ManifestError, match="重复项"):
        Manifest(
            name="alpha",
            source=tmp_path / "manifest.toml",
            models=("a.models", "a.models"),
        )
    with pytest.raises(ManifestError, match="非空字符串"):
        Manifest(name="alpha", source=tmp_path / "manifest.toml", module="")
    with pytest.raises(ManifestError, match="v<数字>"):
        Manifest(
            name="alpha",
            source=tmp_path / "manifest.toml",
            graphql_versions=("v1.2",),
        )


def test_invalid_toml_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha", 'name = "alpha"\nmodels = [\n')
    with pytest.raises(ManifestError, match="TOML 解析失败"):
        load_manifest(path)


def test_manifests_are_sorted_by_directory_name(tmp_path: Path) -> None:
    _write(tmp_path, "zeta", 'name = "zeta"\n')
    _write(tmp_path, "alpha", 'name = "alpha"\n')
    _write(tmp_path, "mike", 'name = "mike"\n')

    assert [m.name for m in load_manifests(tmp_path)] == ["alpha", "mike", "zeta"]


def test_directory_without_manifest_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", 'name = "alpha"\n')
    (tmp_path / "not-a-plugin").mkdir()

    assert [m.name for m in load_manifests(tmp_path)] == ["alpha"]


def test_empty_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="没有任何"):
        load_manifests(tmp_path)


def test_defaults_fill_optional_fields(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha", 'name = "alpha"\n')
    manifest = load_manifest(path)
    assert manifest.models == ()
    assert manifest.tasks == ()
    assert manifest.errors == ()
    assert manifest.aggregate is True
    assert manifest.migrations is None
    assert manifest.graphql_versions == ()
    assert manifest.config == ()


def test_extended_fields_are_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "alpha",
        'name = "alpha"\n'
        'migrations = "alpha.migrations"\n'
        'graphql_versions = ["v1", "v2"]\n'
        'config = ["api_key", "retries"]\n',
    )
    manifest = load_manifest(path)
    assert manifest.migrations == "alpha.migrations"
    assert manifest.graphql_versions == ("v1", "v2")
    assert manifest.config == ("api_key", "retries")


def test_invalid_graphql_version_is_rejected(tmp_path: Path) -> None:
    """版本名会进端点路径，只允许 v<数字>（排除 ``../`` 之类的路径注入面）。"""
    path = _write(
        tmp_path, "alpha", 'name = "alpha"\ngraphql_versions = ["2"]\n'
    )
    with pytest.raises(ManifestError, match="v<数字>"):
        load_manifest(path)
