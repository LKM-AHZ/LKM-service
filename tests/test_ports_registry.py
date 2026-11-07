"""端口注册表：绑定冲突与单端口隔离。

端口是跨模块（app↔auth）同步沟通的唯一通道，因此「谁被绑定」不能取决于装配顺序：
同名端口被第二个实现覆盖时，鉴权类端口（``authz``/``authz_session``）会静默换成另一个
实现，运行时看不出任何异常。
"""

from __future__ import annotations

import pytest

from core.ports import registry


def _fresh_name() -> str:
    return "test.ports.conflict"


def test_rebinding_to_a_different_impl_is_rejected() -> None:
    name = _fresh_name()
    first, second = object(), object()
    try:
        registry.install(name, first)
        with pytest.raises(registry.PortConflict, match="拒绝覆盖"):
            registry.install(name, second)
        assert registry.get(name) is first
    finally:
        registry.reset(name)


def test_rebinding_the_same_object_is_idempotent() -> None:
    name = _fresh_name()
    impl = object()
    try:
        registry.install(name, impl)
        registry.install(name, impl)
        assert registry.get(name) is impl
    finally:
        registry.reset(name)


def test_reset_can_target_a_single_port() -> None:
    """单端口 reset 不得清掉其它已装配端口（测试隔离常用路径）。"""
    name = _fresh_name()
    neighbour = "test.ports.neighbour"
    try:
        registry.install(name, object())
        registry.install(neighbour, object())
        registry.reset(name)
        assert not registry.is_bound(name)
        assert registry.is_bound(neighbour)
    finally:
        registry.reset(name)
        registry.reset(neighbour)
