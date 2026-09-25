"""worker 订阅配置测试：订阅拓扑、points 三订阅扇出、handler 注册。"""

from app.core import messaging, task_registry, worker


def test_subscription_constants() -> None:
    assert worker.SEND_SUBSCRIPTION == "send"
    assert worker.NOTIFY_SUBSCRIPTION == "notify"
    assert worker.POINTS_REWARD_SUBSCRIPTION == "points-reward"
    assert worker.POINTS_STATS_SUBSCRIPTION == "points-stats"
    assert worker.POINTS_TASKS_SUBSCRIPTION == "points-tasks"
    assert worker.USER_INVALIDATE_SUBSCRIPTION == "user-invalidate"
    assert worker.JOBS_SUBSCRIPTION == "jobs"
    assert worker.DLQ == messaging.TOPIC_DLQ


def test_points_three_subscriptions_share_one_topic() -> None:
    subs = [
        messaging.SUB_POINTS_REWARD,
        messaging.SUB_POINTS_STATS,
        messaging.SUB_POINTS_TASKS,
    ]
    assert len({s.topic for s in subs}) == 1
    assert {s.name for s in subs} == {"points-reward", "points-stats", "points-tasks"}


def test_routing_key_topic_map_covers_all_events() -> None:
    expected = {
        messaging.RKEY_SEND_CODE,
        messaging.RKEY_SEND_MAGIC,
        messaging.RKEY_NOTIFY,
        messaging.RKEY_POINTS,
        messaging.RKEY_USER_UPDATED,
        messaging.RKEY_USER_BANNED,
        messaging.RKEY_USER_SESSION_REVOKE,
        messaging.RKEY_CLEANUP,
        messaging.RKEY_RECONCILE,
        messaging.RKEY_ANALYTICS,
        messaging.RKEY_OPS_DAILY,
        messaging.RKEY_CONTENT_PUBLISHED,
        messaging.RKEY_CONTENT_UPDATED,
        messaging.RKEY_CONTENT_DELETED,
        messaging.RKEY_AUDIT_LOGIN_FAIL,
        messaging.RKEY_AUDIT_PERMISSION_CHANGE,
    }
    assert set(messaging.ROUTING_KEY_TOPICS) == expected


def test_subscription_definitions() -> None:
    """订阅定义（唯一源 messaging.SUBSCRIPTIONS）：名 → 关注 routing_key。"""
    topo = {s.name: list(s.routing_keys) for s in messaging.SUBSCRIPTIONS.values()}
    assert topo["send"] == [messaging.RKEY_SEND_CODE, messaging.RKEY_SEND_MAGIC]
    assert messaging.RKEY_NOTIFY in topo["notify"]
    assert messaging.RKEY_CLEANUP in topo["jobs"]
    assert messaging.RKEY_RECONCILE in topo["jobs"]
    assert set(topo) >= {"points-reward", "points-stats", "points-tasks"}
    # user.* 三事件归 user-invalidate 订阅
    ui = set(topo["user-invalidate"])
    assert {
        messaging.RKEY_USER_UPDATED,
        messaging.RKEY_USER_BANNED,
        messaging.RKEY_USER_SESSION_REVOKE,
    } <= ui
    # content.* 三事件归 content-index 订阅（外部检索索引增量同步，B1）
    assert {
        messaging.RKEY_CONTENT_PUBLISHED,
        messaging.RKEY_CONTENT_UPDATED,
        messaging.RKEY_CONTENT_DELETED,
    } <= set(topo["content-index"])
    # §5.2 audit.* 家族：每种审计语义一个 topic + 一个订阅
    assert topo["audit"] == [messaging.RKEY_AUDIT_LOGIN_FAIL]
    assert topo["audit-permission"] == [messaging.RKEY_AUDIT_PERMISSION_CHANGE]


def test_registry_handlers_registered() -> None:
    task_registry.import_task_modules()
    assert task_registry.handlers_for("send")["send_code"]
    assert task_registry.handlers_for("send")["send_magic_link"]
    assert task_registry.handlers_for("notify")["notify_upload"]
    assert task_registry.handlers_for("jobs")["cleanup_expired_uploads"]
    assert task_registry.handlers_for("jobs")["reconcile_blog_repos"]
    assert task_registry.handlers_for("user-invalidate")["invalidate_user_snap"]
