"""worker 进程 OTel 变量静态验收（2026-09-19 真机验证暴露后补）。

背景：backend/auth 是 ASGI app，各自在 environment 里显式声明了 ``LKM_OTEL_*``；而
worker / scheduler / outbox / dlq 进程**不是 ASGI app**（它们只 ``python -m
app.core.worker_*`` 起消费循环），此前 compose 完全没给它们下发 OTel 变量 —— 于是即便
`LKM_OTEL_ENABLED=true`，``pulsar.consume`` / ``pulsar.publish`` 等 span 一条也采不到，
「trace_id 贯穿 Pulsar/调度」在部署层面就不可能成立。真机验证 trace 贯穿时才发现。

代码侧已由 ``app.core.worker._consume`` 等入口调 ``setup_tracing()`` 补齐；本文件守的是
**部署侧**：新增 worker 服务忘了继承 ``x-otel-env`` 锚点即变红。

按**规则**断言（不照抄当前取值，见路线图 §8 #26 的教训）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]  # LKM-Website
_COMPOSE = _ROOT / "docker-compose.yml"

_OTEL_KEYS = (
    "LKM_OTEL_ENABLED",
    "LKM_OTEL_EXPORTER_OTLP_ENDPOINT",
    "LKM_OTEL_SAMPLE_RATIO",
)


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _workers(services: dict[str, Any]) -> dict[str, Any]:
    return {name: svc for name, svc in services.items() if name.startswith("worker")}


def test_every_worker_inherits_otel_env(services: dict[str, Any]) -> None:
    workers = _workers(services)
    assert workers, "未找到任何 worker 服务，锚点断言失去意义"
    missing = {
        name: [key for key in _OTEL_KEYS if key not in (svc.get("environment") or {})]
        for name, svc in workers.items()
    }
    missing = {name: keys for name, keys in missing.items() if keys}
    assert not missing, (
        f"这些 worker 缺 OTel 变量（新增服务请在 environment 加 `<<: *otel-env`）：{missing}"
    )


def test_worker_otel_values_come_from_one_anchor(services: dict[str, Any]) -> None:
    workers = _workers(services)
    value_sets = {
        name: tuple(sorted((key, str(svc["environment"][key])) for key in _OTEL_KEYS))
        for name, svc in workers.items()
    }
    distinct = set(value_sets.values())
    assert len(distinct) == 1, (
        f"worker 的 OTel 取值应全部来自同一锚点，实测有 {len(distinct)} 套：{value_sets}"
    )


@pytest.mark.parametrize("name", ["backend", "auth"])
def test_asgi_services_declare_otel_env(services: dict[str, Any], name: str) -> None:
    env = services[name].get("environment") or {}
    missing = [key for key in _OTEL_KEYS if key not in env]
    assert not missing, f"{name} 缺 OTel 变量：{missing}"
