"""content-index worker 的部署侧验收（B1：content.* 事件 → 检索引擎索引同步）。

被守的**规则**（不照抄当前取值，见路线图 §8 #26 的教训）：

- compose 有 ``worker-content-index``，其 command 指向 ``core.worker_content_index``；
- postgres / redis 使用 ``condition: service_healthy``；broker 由消息层重连，以便切换实现；
- 继承 ``x-otel-env`` 锚点（与 ``test_compose_otel`` 同一规则，此处再钉一次，防锚点被换掉）；
- k8s base 清单里有同名 Deployment——compose 与 k8s 双轨一致（§9.6 既定要求）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"
_WORKERS_YAML = _ROOT / "deploy" / "k8s" / "base" / "app" / "workers.yaml"

_SERVICE = "worker-content-index"
_MODULE = "boot.workers.content_index"
_REQUIRED_DEPS = ("postgres", "redis")
_OTEL_KEY = "LKM_OTEL_ENABLED"


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def test_service_exists_with_entry_point(services: dict[str, Any]) -> None:
    svc = services.get(_SERVICE)
    assert svc is not None, f"compose 缺 {_SERVICE} 服务（content.* 事件将无人消费）"
    command = svc.get("command") or []
    assert _MODULE in " ".join(str(part) for part in command), (
        f"{_SERVICE} 的 command 应指向 {_MODULE}，实测 {command}"
    )


def test_service_waits_for_healthy_middleware(services: dict[str, Any]) -> None:
    depends = services[_SERVICE].get("depends_on") or {}
    missing = [
        name
        for name in _REQUIRED_DEPS
        if (depends.get(name) or {}).get("condition") != "service_healthy"
    ]
    assert not missing, (
        f"{_SERVICE} 缺 `condition: service_healthy` 的依赖：{missing}（启动竞态）"
    )
    assert "pulsar" not in depends
    env = services[_SERVICE]["environment"]
    assert "LKM_MESSAGE_BUS" in env and "LKM_RABBITMQ_URL" in env


def test_service_inherits_otel_anchor(services: dict[str, Any]) -> None:
    env = services[_SERVICE].get("environment") or {}
    assert _OTEL_KEY in env, f"{_SERVICE} 未继承 OTel 锚点（缺 {_OTEL_KEY}）"


def test_k8s_has_matching_deployment() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_WORKERS_YAML.read_text(encoding="utf-8"))
        if doc
    ]
    deployments = {
        doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "Deployment"
    }
    assert _SERVICE in deployments, (
        f"k8s base 清单缺 {_SERVICE} Deployment（compose 与 k8s 双轨须一致）"
    )
    containers = deployments[_SERVICE]["spec"]["template"]["spec"]["containers"]
    assert any(
        _MODULE in " ".join(str(part) for part in (c.get("command") or []))
        for c in containers
    ), f"{_SERVICE} 的 k8s command 未指向 {_MODULE}"
