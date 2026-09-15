"""M5 7.2.5：jobs worker 的 user_dim cron handler 的 Prefect 触发 / fail-open 回落语义。

不触网、不起 server：直接 monkeypatch ``prefect.deployments.run_deployment`` 与既有
``reconcile_user_dim_periodic``，断言三条分支：
- 开关关 → 直调（零回归）；
- 开关开 + 触发成功 → 只触发 deployment（deployment 名/参数/timeout=0），不直调；
- 开关开 + 触发失败 → 回落直调（crash-safety 对账不漏跑）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.modules.auth import tasks as auth_tasks
from app.modules.auth import user_dim_sync


@pytest.fixture
def _spy_direct(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"direct": 0}

    async def _fake_periodic() -> int:
        calls["direct"] += 1
        return 0

    monkeypatch.setattr(
        user_dim_sync, "reconcile_user_dim_periodic", _fake_periodic
    )
    return calls


async def test_flag_off_calls_direct_only(
    monkeypatch: pytest.MonkeyPatch, _spy_direct: dict[str, int]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", False)
    seen: list[object] = []
    monkeypatch.setattr(
        "prefect.deployments.run_deployment",
        lambda *a, **k: seen.append((a, k)),
    )

    await auth_tasks.reconcile_user_dim()

    assert _spy_direct["direct"] == 1
    assert seen == []


async def test_flag_on_triggers_deployment(
    monkeypatch: pytest.MonkeyPatch, _spy_direct: dict[str, int]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", True)
    monkeypatch.setattr(settings, "prefect_deployment", "user-dim-reconcile/lkm")
    monkeypatch.setattr(settings, "prefect_api_url", "http://prefect-server:4200/api")
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect-server:4200/api")
    captured: dict[str, object] = {}

    async def _fake_run_deployment(name: str, **kwargs: object) -> None:
        captured.update(name=name, **kwargs)

    monkeypatch.setattr("prefect.deployments.run_deployment", _fake_run_deployment)

    await auth_tasks.reconcile_user_dim()

    assert _spy_direct["direct"] == 0
    assert captured["name"] == "user-dim-reconcile/lkm"
    assert captured["timeout"] == 0  # 创建即返，不阻塞 worker（JOB_TIMEOUT_S=120）
    params = captured["parameters"]
    assert isinstance(params, dict) and params["mode"] == "reconcile"


async def test_trigger_failure_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch, _spy_direct: dict[str, int]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", True)
    monkeypatch.setattr(settings, "prefect_deployment", "user-dim-reconcile/lkm")
    monkeypatch.setattr(settings, "prefect_api_url", "http://prefect-server:4200/api")
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect-server:4200/api")

    async def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("prefect server unreachable")

    monkeypatch.setattr("prefect.deployments.run_deployment", _boom)

    await auth_tasks.reconcile_user_dim()

    assert _spy_direct["direct"] == 1  # fail-open 回落，保证对账不漏跑
