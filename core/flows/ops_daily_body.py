"""运营日报的纯查询体（无 Prefect 依赖，可被 flow 或回落直调复用）。

蓝图 §5.5/§6.4 把「运营日报」列为 Prefect flow。**最小版**只聚合**已经落库**的数据，不引入
新的统计链路、不新建表：

- ClickHouse ``lkm.event_failures``（outbox 永久失败事件，analytics flow 每日已灌）
- ClickHouse ``lkm.audit_logs``（auth 行为审计，同上）
- 业务库 ``content_items``（按 ``created_at`` 的日新增数）

刻意**不碰**在线口径（活跃用户 / 发帖趋势已有 admin 端点实时算）：日报的定位是**留痕**，
不是再造一套实时统计。

容错：任一路不可用（CH 未启用/查询失败）只让该路为空并告警，不影响其余——日报缺一路仍比
整个 flow 失败有用。
"""

from __future__ import annotations

import logging

from core import clickhouse

logger = logging.getLogger("lkm.flows.ops_daily")


async def _ch_counts_by_day(sql: str, days: int) -> dict[str, int]:
    """跑一条「按日计数」的 CH 查询，返回 ``{YYYY-MM-DD: count}``；不可用则空 dict。"""
    if not clickhouse.is_enabled():
        return {}
    try:
        client = await clickhouse.get_client()
        result = await client.query(sql)
    except Exception:
        logger.warning("运营日报：ClickHouse 查询失败，该路置空", exc_info=True)
        return {}
    out: dict[str, int] = {}
    for row in clickhouse.result_rows(result):
        if len(row) < 2:
            continue
        out[str(row[0])] = int(row[1])
    return out


async def count_event_failures_by_day(days: int) -> dict[str, int]:
    """``lkm.event_failures`` 近 N 天按日（按 ``folded_at``，即事件被判定永久失败的时刻）。"""
    # days 是 int（已由调用方校验为正），直接内插；不接受外部字符串，无注入面。
    sql = (
        "SELECT toDate(folded_at) AS d, count() AS n "
        "FROM lkm.event_failures "
        f"WHERE folded_at >= now() - INTERVAL {int(days)} DAY "
        "GROUP BY d ORDER BY d"
    )
    return await _ch_counts_by_day(sql, days)


async def count_audit_logs_by_day(days: int) -> dict[str, int]:
    """``lkm.audit_logs`` 近 N 天按日（按 ``created_at``）。"""
    sql = (
        "SELECT toDate(created_at) AS d, count() AS n "
        "FROM lkm.audit_logs "
        f"WHERE created_at >= now() - INTERVAL {int(days)} DAY "
        "GROUP BY d ORDER BY d"
    )
    return await _ch_counts_by_day(sql, days)


async def count_content_created_by_day(days: int) -> dict[str, int]:
    """业务库近 N 天按日新增内容数（只计当前未软删的行）。

    业务表结构只有 app 侧知道，故实现由 ``app.bootstrap`` 绑定到
    ``core.ports.content_stats``；未装配 app 的进程调用会立刻抛 PortNotBound，
    不会把「内容新增」静默当 0。
    """
    from core.ports.content_stats import count_content_created_by_day as _count

    return await _count(days)


async def collect_daily_report(*, days: int) -> dict[str, object]:
    """汇总一份日报：三路按日序列 + 各自的合计。

    任一路为空（数据源不可用或该窗口确实无数据）不视为失败——调用方据各序列自行判断。
    """
    if days <= 0:
        raise ValueError(f"days 必须为正数，收到 {days}")

    failures = await count_event_failures_by_day(days)
    audits = await count_audit_logs_by_day(days)
    content = await count_content_created_by_day(days)

    report: dict[str, object] = {
        "window_days": days,
        "event_failures_by_day": failures,
        "audit_logs_by_day": audits,
        "content_created_by_day": content,
        "totals": {
            "event_failures": sum(failures.values()),
            "audit_logs": sum(audits.values()),
            "content_created": sum(content.values()),
        },
    }
    logger.info(
        "运营日报（近 %d 天）：永久失败事件 %d 条、审计 %d 条、内容新增 %d 条",
        days,
        report["totals"]["event_failures"],  # type: ignore[index]
        report["totals"]["audit_logs"],  # type: ignore[index]
        report["totals"]["content_created"],  # type: ignore[index]
    )
    return report
