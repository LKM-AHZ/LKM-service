import pytest

from app.core import scheduler
from app.core.config import settings
from app.core.task_registry import cron_jobs


def test_ensure_tasks_registered_survives_partial_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防回潮：handler 表「非空」不等于「已全量导入」。

    任一 ``tasks.py`` 被单独导入（如测试只导入 ``notification.tasks``）只会填 handler 表、
    不填 cron 表；旧 guard 据此判「已注册」而跳过全量导入，``build_scheduler()`` 会拿到
    0 个 job（2026-09-18 在 ``test_notification.py`` 先收集时暴露）。
    """
    from app.core import task_registry

    calls: list[int] = []
    monkeypatch.setattr(task_registry, "_tasks_imported", False)
    monkeypatch.setattr(task_registry, "_TASK_HANDLERS", {"notify": {"x": object()}})
    monkeypatch.setattr(task_registry, "_CRON_JOBS", [])
    monkeypatch.setattr(task_registry, "import_task_modules", lambda: calls.append(1))

    task_registry.ensure_tasks_registered()

    assert calls == [1], "handler 表非空不得让全量导入被跳过"


def test_import_task_modules_marks_import_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量导入须置标志（模块被 ``sys.modules`` 缓存，重导不会再触发注册）。"""
    from app.core import task_registry

    monkeypatch.setattr(task_registry, "_tasks_imported", False)
    task_registry.import_task_modules()
    assert task_registry._tasks_imported is True


def test_scheduler_has_cron_jobs() -> None:
    s = scheduler.build_scheduler()
    jobs = s.get_jobs()
    # 与注册表逐条对齐（不写死数量：写穿模式下 flush cron 不注册，见 content/tasks.py）
    assert len(jobs) == len(cron_jobs())
    triggers = {(j.id, type(j.trigger).__name__) for j in jobs}
    assert ("cleanup_expired_uploads", "CronTrigger") in triggers
    assert ("reconcile_blog_repos", "CronTrigger") in triggers
    assert ("reconcile_user_dim", "CronTrigger") in triggers  # B0.2 周期增量对账(每天)
    assert ("analytics_export", "CronTrigger") in triggers  # M5 7.2.6 ClickHouse 分析导出(每天)
    assert (
        "purge_stale_view_logs",
        "CronTrigger",
    ) in triggers  # M6.6 浏览记录保留期清理(每天)
    # M6.10 互动计数增量落库(每分钟)——写穿模式下写路径已直接落库，该 cron 不注册
    assert ("flush_content_counters", "CronTrigger") in triggers or (
        settings.counters_write_through
    )
    assert (
        "reconcile_content_counts",
        "CronTrigger",
    ) in triggers  # M6.10 互动计数对账(每 15 分钟)
    assert ("fanout_feed_items", "CronTrigger") in triggers  # M6.11 时间线写扩散(每 2 分钟)


def test_scheduler_fire_fns_match_worker_handler_keys() -> None:
    """调度器每个 cron job 发布的 fn 必须能命中 worker 的 handler 键。

    若 fn 与 worker 注册表键不一致，worker 按其 fn 查表得 None 会当"未知任务"
    丢弃，cron 永不执行。此测试直接检查 build_scheduler 里每个 job 的 kwargs.fn。
    """
    from app.core.worker import run_default_worker

    # 期望的 fn ↔ worker run_default_worker 各队列 handler 键并集
    expect_fns = {
        "cleanup_expired_uploads",
        "reconcile_blog_repos",
        "reconcile_user_dim",
        "export_analytics_clickhouse",  # M5 7.2.6
        "purge_stale_view_logs",  # M6.6
        "reconcile_content_counts",  # M6.10（两种模式都注册）
        "fanout_feed_items",  # M6.11
        "run_ops_daily",  # 运营日报（蓝图 §5.5/§6.4）
    }
    if not settings.counters_write_through:
        expect_fns.add("flush_content_counters")  # 仅回退（write-behind）模式注册
    s = scheduler.build_scheduler()
    job_fns = {str(j.kwargs.get("fn")) for j in s.get_jobs()}
    assert job_fns == expect_fns
    # 引用一下 run_default_worker 避免"未使用"；真实契约由集成测试最终验证
    assert callable(run_default_worker)
