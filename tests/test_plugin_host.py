"""插件宿主：校验、定序、外部入口点发现。

三条规则各自对应一类「启动不报错、运行时才崩」的故障，因此都必须 fail-fast：
重名（谁生效取决于导入顺序）、requires 无法满足（缺命令/缺字段）、依赖成环（无解）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.modules import spec as app_spec
from core.plugins import host
from core.plugins.manifest import Manifest, load_manifests


def _manifest(
    name: str,
    *,
    provides: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    module: str | None = "demo_plugin",
) -> Manifest:
    return Manifest(
        name=name,
        source=Path(f"/tmp/{name}/manifest.toml"),
        module=module,
        provides=provides,
        requires=requires,
    )


def test_duplicate_names_are_rejected() -> None:
    plugins = host.PluginSet((_manifest("dup"), _manifest("dup")))
    with pytest.raises(host.PluginError, match="重名"):
        plugins.validate()


def test_unresolvable_requirement_is_rejected() -> None:
    plugins = host.PluginSet((_manifest("a", requires=("nobody.provides",)),))
    with pytest.raises(host.PluginError, match="requires 无法满足"):
        plugins.validate()


def test_cycle_is_rejected() -> None:
    plugins = host.PluginSet(
        (
            _manifest("a", provides=("a.x",), requires=("b.x",)),
            _manifest("b", provides=("b.x",), requires=("a.x",)),
        )
    )
    with pytest.raises(host.PluginError, match="成环"):
        plugins.validate()


def test_install_order_puts_dependencies_first() -> None:
    plugins = host.PluginSet(
        (
            _manifest("consumer", requires=("core.x",)),
            _manifest("provider", provides=("core.x",)),
        )
    )
    plugins.validate()
    assert [m.name for m in plugins.install_order()] == ["provider", "consumer"]


def test_order_without_requires_is_stable() -> None:
    """内置模块没有 requires，定序不得打乱清单顺序——那会改变路由注册顺序。"""
    names = tuple(f"m{i}" for i in range(6))
    plugins = host.PluginSet(tuple(_manifest(n) for n in names))
    assert tuple(m.name for m in plugins.install_order()) == names


def test_builtin_manifests_validate_cleanly() -> None:
    builtin = load_manifests(Path(app_spec.__file__).parent)
    host.PluginSet(builtin).validate()


class _FakeEntryPoint:
    def __init__(self, name: str, factory: Any) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> Any:
        return self._factory


def test_discover_reads_manifests_from_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest("demo")
    monkeypatch.setattr(
        host, "entry_points", lambda group: [_FakeEntryPoint("demo", lambda: manifest)]
    )

    assert host.discover_entry_points() == (manifest,)


def test_discover_returns_empty_without_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "entry_points", lambda group: [])
    assert host.discover_entry_points() == ()


def test_discover_rejects_non_manifest_return(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        host, "entry_points", lambda group: [_FakeEntryPoint("bad", lambda: {"name": "x"})]
    )
    with pytest.raises(host.PluginError, match="期望"):
        host.discover_entry_points()


def test_discover_requires_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest("demo", module=None)
    monkeypatch.setattr(
        host, "entry_points", lambda group: [_FakeEntryPoint("demo", lambda: manifest)]
    )
    with pytest.raises(host.PluginError, match="缺少 module"):
        host.discover_entry_points()


def test_discover_rejects_name_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest("demo")
    monkeypatch.setattr(
        host, "entry_points", lambda group: [_FakeEntryPoint("other", lambda: manifest)]
    )
    with pytest.raises(host.PluginError, match="不一致"):
        host.discover_entry_points()


def test_discover_wraps_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise ImportError("broken plugin")

    monkeypatch.setattr(
        host, "entry_points", lambda group: [_FakeEntryPoint("broken", _boom)]
    )
    with pytest.raises(host.PluginError, match="加载失败"):
        host.discover_entry_points()
