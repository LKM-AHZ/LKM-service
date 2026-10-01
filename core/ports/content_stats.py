"""
内容统计端口：core 侧的跨域报表（如运营日报）读业务表时使用。
core 不得知道业务表结构，故该读能力由 **app 侧** ``app.bootstrap`` 绑定实现。
运行时报表在 ``worker-scheduler`` 进程（boot 装配，app 已导入），端口可用；
若在未装配 app 的进程误触发，:class:`~core.ports.registry.PortNotBound` 会立刻炸出，
不会静默把日报内容数当 0。
"""

from __future__ import annotations

from core.ports.registry import get


async def count_content_created_by_day(days: int) -> dict[str, int]:
    """业务库近 N 天按日新增内容数（只计未软删行），返回 ``{YYYY-MM-DD: count}``。"""
    return await get("content_stats").count_content_created_by_day(days)
