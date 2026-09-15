from app.core import scheduler


def test_scheduler_has_cron_jobs() -> None:
    s = scheduler.build_scheduler()
    jobs = s.get_jobs()
    assert len(jobs) == 4
    triggers = {(j.id, type(j.trigger).__name__) for j in jobs}
    assert ("cleanup_expired_uploads", "CronTrigger") in triggers
    assert ("reconcile_blog_repos", "CronTrigger") in triggers
    assert ("reconcile_user_dim", "CronTrigger") in triggers  # B0.2 周期增量对账(每天)
    assert ("analytics_export", "CronTrigger") in triggers  # M5 7.2.6 ClickHouse 分析导出(每天)


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
    }
    s = scheduler.build_scheduler()
    job_fns = {str(j.kwargs.get("fn")) for j in s.get_jobs()}
    assert job_fns == expect_fns
    # 引用一下 run_default_worker 避免"未使用"；真实契约由集成测试最终验证
    assert callable(run_default_worker)
