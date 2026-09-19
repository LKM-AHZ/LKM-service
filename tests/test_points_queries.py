"""积分查询端点（Task6）：排行榜 period + title、成就/任务/兑换列表。

直接驱动 service 函数验证聚合与标题逻辑；seed 数据经 conftest PG schema-per-test 建表后
由 seed 函数写入。遵循项目 async / PG 测试惯例。
"""

import datetime
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.points.errors import PointsErr
from app.modules.points.models import (
    Achievement,
    PointsLedger,
    UserAchievement,
    UserBalance,
)
from app.modules.points.schemas import (
    AchievementOut,
    ExchangeItemOut,
    LeaderboardEntry,
    TaskOut,
)
from app.modules.points.seed import (
    seed_achievements,
    seed_exchange_items,
    seed_tasks,
)
from app.modules.points.service import (
    leaderboard,
    list_achievements,
    list_exchange_items,
    list_tasks,
)
from auth.security import create_access_token
from tests.conftest import auth_user_uid


async def _create_user(
    db: AsyncSession,
    auth_db: AsyncSession,
    username: str,
    nickname: str = "",
) -> uuid.UUID:
    """在 auth realm 建用户返其 uuid 主键，并在业务 points 表建 balance=0 行。"""
    uid = (
        await auth_user_uid(
            auth_db,
            username=username,
            email=f"{username}@example.com",
            nickname=nickname or None,
        )
    ).id
    db.add(UserBalance(user_id=uid, balance=0))
    await db.flush()
    return uid


async def _mk_ledger(
    db: AsyncSession, user_id: uuid.UUID, deltas: list[int]
) -> None:
    for i, d in enumerate(deltas):
        db.add(
            PointsLedger(
                user_id=user_id,
                delta=d,
                balance_after=d,
                reason="test",
                ref_type="test",
                ref_id=f"q-{user_id}-{i}",
            )
        )
    await db.flush()


async def test_seed_yields_nonempty_lists(db: AsyncSession) -> None:
    """seed 后成就/任务/兑换三表各返回非空定义列表。"""
    await seed_achievements(db)
    await seed_tasks(db)
    await seed_exchange_items(db)
    await db.commit()

    achs = await list_achievements(db)
    tasks = await list_tasks(db)
    items = await list_exchange_items(db)

    assert len(achs) == 12
    assert len(tasks) == 5
    assert len(items) == 6
    assert all(isinstance(a, AchievementOut) for a in achs)
    assert all(isinstance(t, TaskOut) for t in tasks)
    assert all(isinstance(i, ExchangeItemOut) for i in items)
    # 详情字段齐全（前端 i18n 需要的 key 原样输出）
    assert achs[0].name_key.startswith("contributionData.achievements.a")
    assert tasks[0].title_key.startswith("contributionData.tasks.t")
    assert items[0].name_key.startswith("contributionData.exchangeItems.e")


async def test_leaderboard_total_sort_and_title(db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """total 按 UserBalance 余额降序，且每项带 title（默认 active）。"""
    await seed_achievements(db)
    await db.commit()
    low_id = await _create_user(db, auth_db, "money_low", "低")
    await _create_user(db, auth_db, "money_high", "高")
    (await db.get(UserBalance, low_id)).balance = 10
    await db.flush()

    rows, total = await leaderboard(db)
    assert total == len(rows)
    assert isinstance(rows[0], LeaderboardEntry)
    assert all(entry.title for entry in rows)  # 每项都带 title，默认 active
    assert all(entry.balance >= 0 for entry in rows)


async def test_leaderboard_daily_weekly_aggregate(db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """daily 近 24h / weekly 近 7 天按 points_ledger.delta>0 汇总降序。"""
    a_id = await _create_user(db, auth_db, "agg_a", "A")
    b_id = await _create_user(db, auth_db, "agg_b", "B")
    await _create_user(db, auth_db, "agg_c", "C")
    # A 近窗口 +30，B 近窗口 +10，C 无流水（不出现在 daily/weekly 榜）
    await _mk_ledger(db, a_id, [20, 10])
    await _mk_ledger(db, b_id, [10])
    await db.commit()

    daily, _total = await leaderboard(db, period="daily")
    assert [r.user_id for r in daily] == [a_id, b_id]
    assert daily[0].balance == 30
    assert daily[1].balance == 10

    weekly, _total = await leaderboard(db, period="weekly")
    assert [r.user_id for r in weekly] == [a_id, b_id]
    assert weekly[0].balance == 30


async def test_leaderboard_invalid_period(db: AsyncSession) -> None:
    """非法 period 抛 INVALID_PERIOD。"""
    with pytest.raises(BizError) as exc:
        await leaderboard(db, period="monthly")
    assert exc.value.errcode == PointsErr.INVALID_PERIOD


async def test_leaderboard_daily_filters_stale_ledger(db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """超出 daily 窗口（长期无流水）的余额用户不进 daily 榜。"""
    old_id = await _create_user(db, auth_db, "old_user", "老")
    await _create_user(db, auth_db, "new_user", "新")
    (await db.get(UserBalance, old_id)).balance = 500
    # 老用户一条很久前的流水（超出 7 天窗口）
    old_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    db.add(
        PointsLedger(
            user_id=old_id,
            delta=100,
            balance_after=100,
            reason="old",
            ref_type="test",
            ref_id="old-1",
            created_at=old_at,
        )
    )
    await db.commit()

    daily, _t = await leaderboard(db, period="daily")
    # 老用户仅余额高但日榜无近期流水 → 不出现
    assert all(r.user_id != old_id for r in daily)
    weekly, _t = await leaderboard(db, period="weekly")
    assert all(r.user_id != old_id for r in weekly)


async def test_leaderboard_title_hardcore(db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """解锁 a7（accepted_answers）→ title hardcore。"""
    await seed_achievements(db)
    await db.commit()
    uid = await _create_user(db, auth_db, "answer_master", "大神")
    (await db.get(UserBalance, uid)).balance = 50
    a7 = (
        (await db.execute(select(Achievement).where(Achievement.key == "a7")))
        .scalars()
        .first()
    )
    assert a7 is not None
    ua = UserAchievement(user_id=uid, achievement_id=a7.id, progress=20, unlocked=True)
    db.add(ua)
    await db.commit()

    rows, _t = await leaderboard(db)
    entry = next(r for r in rows if r.user_id == uid)
    assert entry.title == "hardcore"


async def test_leaderboard_title_batch_priority(db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """批量 title：多用户各按已解锁成就合成，且同用户多成就按优先级取更高级。"""
    await seed_achievements(db)
    await db.commit()
    # 用户 A 仅解锁 a7 → hardcore；用户 B 仅解锁 a8 → fileExpert
    a_id = await _create_user(db, auth_db, "batch_a", "甲")
    b_id = await _create_user(db, auth_db, "batch_b", "乙")
    # 用户 C 同时解锁 a7+a8 → 仍为 higher 优先级 hardcore
    c_id = await _create_user(db, auth_db, "batch_c", "丙")
    for uid in (a_id, b_id, c_id):
        (await db.get(UserBalance, uid)).balance = 100
    a7 = (
        (await db.execute(select(Achievement).where(Achievement.key == "a7")))
        .scalars()
        .first()
    )
    a8 = (
        (await db.execute(select(Achievement).where(Achievement.key == "a8")))
        .scalars()
        .first()
    )
    assert a7 is not None and a8 is not None
    db.add(
        UserAchievement(user_id=a_id, achievement_id=a7.id, progress=20, unlocked=True)
    )
    db.add(
        UserAchievement(user_id=b_id, achievement_id=a8.id, progress=20, unlocked=True)
    )
    db.add(
        UserAchievement(user_id=c_id, achievement_id=a7.id, progress=20, unlocked=True)
    )
    db.add(
        UserAchievement(user_id=c_id, achievement_id=a8.id, progress=20, unlocked=True)
    )
    await db.commit()

    rows, _t = await leaderboard(db)
    title_by_id = {r.user_id: r.title for r in rows}
    assert title_by_id[a_id] == "hardcore"
    assert title_by_id[b_id] == "fileExpert"
    assert title_by_id[c_id] == "hardcore"


async def test_achievements_include_current_user_progress(db: AsyncSession) -> None:
    """list_achievements 返回定义 + 当前用户进度（本函数无登录概念，返回定义即可）。"""
    await seed_achievements(db)
    await db.commit()
    achs = await list_achievements(db)
    assert len(achs) == 12
    # 未解锁时 progress=0 / unlocked=False 默认值
    assert all(a.progress == 0 and not a.unlocked for a in achs)


async def test_tasks_include_current_progress_defaults(db: AsyncSession) -> None:
    """list_tasks 返回定义 + 今日进度默认值。"""
    await seed_tasks(db)
    await db.commit()
    tasks = await list_tasks(db)
    assert len(tasks) == 5
    assert all(t.completed is False for t in tasks)
    assert all(
        t.category in {"checkin", "post", "answer", "like", "file_upload"}
        for t in tasks
    )


async def test_endpoint_exchange_items_public(client, db: AsyncSession) -> None:
    """exchange-items 公开无需登录，seed 后返回兑换列表。"""
    await seed_exchange_items(db)
    await db.commit()
    resp = await client.get("/api/v1/points/exchange-items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    items = body["data"]
    assert len(items) == 6
    assert set(items[0]) >= {"id", "key", "name_key", "points_cost", "stock"}


async def test_endpoint_achievements_and_tasks_require_auth(
    client, db: AsyncSession
) -> None:
    """achievements/tasks 需登录；未带 token 返 403。"""
    resp = await client.get("/api/v1/points/achievements")
    assert resp.status_code == 403
    resp = await client.get("/api/v1/points/tasks")
    assert resp.status_code == 403


async def test_endpoint_achievements_returns_progress_when_authed(
    client, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """携带登录 token 的 achievements 返回 200 + 定义列表。"""
    await seed_achievements(db)
    uid = await _create_user(db, auth_db, "authed_ach", "已登录")
    await db.commit()
    token = create_access_token(user_id=uid, account_level="local", role="member")
    resp = await client.get(
        "/api/v1/points/achievements",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]
    assert len(items) == 12
    assert all("progress" in it and "unlocked" in it for it in items)


async def test_endpoint_leaderboard_period_param(client, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None) -> None:
    """leaderboard 支持 period 参数；非法 period 返 400。"""
    uid = await _create_user(db, auth_db, "lb_user", "榜")
    (await db.get(UserBalance, uid)).balance = 15
    await _mk_ledger(db, uid, [15])
    await db.commit()

    ok = await client.get("/api/v1/points/leaderboard", params={"period": "daily"})
    assert ok.status_code == 200
    assert ok.json()["code"] == 0

    bad = await client.get("/api/v1/points/leaderboard", params={"period": "monthly"})
    assert bad.status_code == 400
    assert bad.json()["code"] == PointsErr.INVALID_PERIOD
