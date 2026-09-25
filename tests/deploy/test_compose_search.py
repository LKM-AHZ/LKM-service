"""检索引擎的部署侧验收（B2：Meilisearch / OpenSearch）。

被守的**规则**（不照抄当前取值，见路线图 §8 #26）：

- 两个引擎都是**可选组件**：必须挂在 ``search`` profile 下——漏掉 profile 会让它们随默认栈
  启动，把「默认零外部依赖」的承诺破坏掉；
- 都是内网服务：只 ``expose``、不 ``ports``（发布宿主端口会把无鉴权的 OpenSearch 暴露出去）；
- 它们的持久化卷在顶层 ``volumes:`` 有声明（compose 不认未声明的具名卷）；
- backend 与 worker-content-index **都**声明 ``LKM_SEARCH_ENGINE``——只配一边会出现
  「读路径走引擎、同步 worker 仍只记账」的静默不一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"

_ENGINES = {
    "meilisearch": "meili_data",
    "opensearch": "opensearch_data",
}
_CONSUMERS = ("backend", "worker-content-index")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]


@pytest.mark.parametrize("name", sorted(_ENGINES))
def test_engine_is_optional_profile(services: dict[str, Any], name: str) -> None:
    svc = services.get(name)
    assert svc is not None, f"compose 缺 {name}（search profile 无法启用）"
    assert "search" in (svc.get("profiles") or []), (
        f"{name} 必须挂在 search profile 下，否则会随默认栈启动"
    )


@pytest.mark.parametrize("name", sorted(_ENGINES))
def test_engine_is_internal_only(services: dict[str, Any], name: str) -> None:
    svc = services[name]
    assert not svc.get("ports"), f"{name} 不应发布宿主端口（内网服务）"


@pytest.mark.parametrize(("name", "volume"), sorted(_ENGINES.items()))
def test_engine_volume_is_declared(
    compose: dict[str, Any], services: dict[str, Any], name: str, volume: str
) -> None:
    mounts = " ".join(str(m) for m in (services[name].get("volumes") or []))
    assert volume in mounts, f"{name} 未挂载 {volume}"
    assert volume in (compose.get("volumes") or {}), f"顶层 volumes 未声明 {volume}"


@pytest.mark.parametrize("name", _CONSUMERS)
def test_consumers_declare_search_engine(
    services: dict[str, Any], name: str
) -> None:
    env = services[name].get("environment") or {}
    assert "LKM_SEARCH_ENGINE" in env, (
        f"{name} 缺 LKM_SEARCH_ENGINE：读路径与索引同步必须同源配置，否则静默不一致"
    )


# ---- k8s 双轨一致性（compose 与 k8s 须同时可选）----


def test_k8s_engines_are_optional_replicas() -> None:
    """k8s 用 replicas: 0 表「可选组件」——与 compose 的 profile 是同一语义的两种表达。"""
    path = _ROOT / "deploy" / "k8s" / "base" / "infra" / "search.yaml"
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    states = {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "StatefulSet"
    }
    assert set(states) == set(_ENGINES), f"k8s 清单应含 {sorted(_ENGINES)} 两个引擎"
    for name, doc in states.items():
        assert doc["spec"]["replicas"] == 0, f"{name} 应默认 replicas: 0（可选组件约定）"


def test_kustomization_includes_search_manifest() -> None:
    path = _ROOT / "deploy" / "k8s" / "base" / "kustomization.yaml"
    kustomization = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "infra/search.yaml" in kustomization["resources"], (
        "search.yaml 未进 kustomization，k8s 侧等于没交付"
    )
