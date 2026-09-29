"""双后端路由（按 key 前缀）与失效广播双发的单元测试。

路由是灰度的地基：**同一前缀恒定落同一后端**，一致性才成立。故这里锁住段边界（``ip`` 不得
误配 ``ipfoo``）、``make_key`` 命名空间剥离、未配置时不动（行为与单后端一致）。
"""

from typing import Any

from app.core import redis as redis_mod
from app.core.config import settings


def _configure(
    monkeypatch: Any, prefixes: str, *, secondary: str = "redis://df:6379/0"
) -> None:
    monkeypatch.setattr(settings, "redis_url_secondary", secondary)
    monkeypatch.setattr(settings, "redis_secondary_prefixes", prefixes)


def test_no_secondary_configured_never_routes(monkeypatch: Any) -> None:
    """未配置第二后端 → 恒不路由（这正是「留空 = 与改造前完全一致」的保证）。"""
    _configure(monkeypatch, "user:snap", secondary="")
    assert redis_mod.is_secondary("lkm:dev:user:snap:123") is False


def test_route_by_make_key_prefix(monkeypatch: Any) -> None:
    _configure(monkeypatch, "user:snap,feed")
    assert redis_mod.is_secondary("lkm:dev:user:snap:abc") is True
    assert redis_mod.is_secondary("lkm:dev:feed:bigv") is True
    assert redis_mod.is_secondary("lkm:dev:content:list:1") is False


def test_route_segment_boundary(monkeypatch: Any) -> None:
    """``ip`` 前缀不得误配 ``ipfoo``（同词头异段）——否则会把不相干的域悄悄切走。"""
    _configure(monkeypatch, "ip")
    assert redis_mod.is_secondary("ip:1.2.3.4") is True
    assert redis_mod.is_secondary("ipfoo") is False


def test_route_bare_key_prefix(monkeypatch: Any) -> None:
    """裸键（无 ``lkm:{env}:`` 命名空间）用字面前缀匹配；带命名空间的同前缀也应命中。"""
    _configure(monkeypatch, "upload,jti:block")
    assert redis_mod.is_secondary("upload:abc") is True
    assert redis_mod.is_secondary("jti:block:xyz") is True
    assert redis_mod.is_secondary("lkm:dev:upload:x") is True


def test_none_key_never_secondary(monkeypatch: Any) -> None:
    """无 key 场景（health 探针、pub/sub）走主后端。"""
    _configure(monkeypatch, "user:snap")
    assert redis_mod.is_secondary(None) is False


def test_route_target_strips_env_namespace(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "env", "prod")
    assert redis_mod._route_target("lkm:prod:feed:bigv") == "feed:bigv"
    assert redis_mod._route_target("upload:x") == "upload:x"  # 裸键原样


async def test_publish_invalidate_fans_out_to_all_backends(monkeypatch: Any) -> None:
    """L1 失效广播须**双发**：被失效的 key 可能落在任一后端，订阅方无从判断归属。

    只发一个后端会让另一个后端的实例漏删本地 L1（陈旧到 L1 TTL 到期才自愈）。
    """
    from app.core import user_cache_events as uce

    sent: list[tuple[str, str]] = []

    class _C:
        def __init__(self, label: str) -> None:
            self.label = label

        async def publish(self, chan: str, key: str) -> None:
            sent.append((self.label, key))

    async def _all() -> list[tuple[str, Any]]:
        return [("primary", _C("primary")), ("secondary", _C("secondary"))]

    monkeypatch.setattr(uce.redis_client, "all_clients", _all)
    await uce.publish_invalidate("lkm:dev:user:snap:1")

    assert [label for label, _ in sent] == ["primary", "secondary"]
    assert all(key == "lkm:dev:user:snap:1" for _, key in sent)
