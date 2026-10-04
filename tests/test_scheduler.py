"""Prefect cron deployment 与消息发布契约。"""

import pytest

from boot import flows_deploy
from boot.assemble import assemble
from core import scheduler, task_registry
from core.config import settings


def test_all_cron_jobs_have_worker_handlers() -> None:
    assemble()
    task_registry.ensure_tasks_registered()
    jobs = task_registry.cron_jobs()
    ids = {job["id"] for job in jobs}
    assert {"cleanup_expired_uploads", "reconcile_user_dim", "analytics_export"} <= ids
    assert "flush_content_counters" in ids
    flush = next(job for job in jobs if job["id"] == "flush_content_counters")
    assert flush["enabled"] is not settings.counters_write_through
    handlers = {
        fn
        for subscription in task_registry._TASK_HANDLERS
        for fn in task_registry.handlers_for(subscription)
    }
    assert {job["fn"] for job in jobs} <= handlers


@pytest.mark.parametrize("enabled", [True, False])
def test_cron_jobs_become_prefect_schedules(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    calls: list[dict] = []

    class _Deployment:
        def deploy(self, **kwargs):
            calls.append(kwargs)
            return "test-id"

    monkeypatch.setattr(flows_deploy, "DEPLOYMENTS", [])
    monkeypatch.setattr(flows_deploy, "assemble", lambda: None)
    monkeypatch.setattr(flows_deploy, "ensure_tasks_registered", lambda: None)
    monkeypatch.setattr(
        flows_deploy,
        "cron_jobs",
        lambda: [{"id": "cleanup", "cron": "*/5 * * * *", "enabled": enabled}],
    )
    monkeypatch.setattr(
        flows_deploy.cron_dispatch_flow,
        "from_source",
        lambda **_kwargs: _Deployment(),
    )

    flows_deploy.main()

    assert calls == [
        {
            "name": "cleanup",
            "work_pool_name": flows_deploy.WORK_POOL,
            "build": False,
            "push": False,
            "cron": "*/5 * * * *",
            "parameters": {"job_id": "cleanup"},
            "concurrency_limit": 1,
            "paused": not enabled,
        }
    ]


async def test_fire_cron_job_uses_registered_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    async def _record(routing_key: str, fn: str) -> None:
        sent.append((routing_key, fn))

    jobs = [
        {
            "id": "cleanup",
            "routing_key": "cron.cleanup",
            "fn": "cleanup",
            "enabled": True,
        }
    ]
    monkeypatch.setattr(scheduler, "_fire", _record)
    monkeypatch.setattr(
        scheduler.task_registry, "ensure_tasks_registered", lambda: None
    )
    monkeypatch.setattr(
        scheduler.task_registry,
        "cron_jobs",
        lambda: jobs,
    )
    await scheduler.fire_cron_job("cleanup")
    assert sent == [("cron.cleanup", "cleanup")]
    jobs[0]["enabled"] = False
    with pytest.raises(ValueError, match="已停用"):
        await scheduler.fire_cron_job("cleanup")
    with pytest.raises(ValueError, match="未知 cron job"):
        await scheduler.fire_cron_job("missing")


async def test_fire_retries_with_one_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, str]] = []
    delays: list[float] = []

    async def _publish(_routing_key: str, payload: dict[str, str]) -> bool:
        sent.append(dict(payload))
        return len(sent) == 3

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(scheduler.messaging, "publish", _publish)
    monkeypatch.setattr(scheduler.asyncio, "sleep", _sleep)
    await scheduler._fire("cron.reconcile", "reconcile_content_counts")

    assert len({item["event_id"] for item in sent}) == 1
    assert delays == [1.0, 2.0]


async def test_fire_failure_marks_flow_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _publish(_routing_key: str, _payload: dict[str, str]) -> bool:
        nonlocal calls
        calls += 1
        return False

    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(scheduler.messaging, "publish", _publish)
    monkeypatch.setattr(scheduler.asyncio, "sleep", _sleep)
    with pytest.raises(RuntimeError, match="发布失败"):
        await scheduler._fire("cron.reconcile", "reconcile_content_counts")
    assert calls == 3
