"""Prefect flow：user_dim 报表宽表增量对账 / 显式回填（M5 7.2.5）。

蓝图《后端规划.md》§调度定案：APScheduler 只做简单 cron 触发入口（``cron.*`` 经总线），
DAG / 失败重试 / 回填由 Prefect flow 承接。本模块不复制 ETL SQL，全部经
``auth.seams`` 复用 auth 的既有 ETL 入口，保持其**命令数恒定 / 跨 realm 双会话 /
幂等**不变量。

mode：
- ``reconcile``：分窗对账至收敛（周期 crash-safety 网；复用 periodic 入口的 Redis 锁语义）；
- ``incremental``：单拍增量对账（调试 / 小步）；
- ``ids``：显式 id 批量 upsert（运维**回填**入口，幂等可重跑）。

CLI（在 worker 容器内手工回填）::

    python -m app.flows.user_dim --backfill --ids 1,2,3
    python -m app.flows.user_dim --mode reconcile --window 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from prefect import flow, task

logger = logging.getLogger("lkm.flows.user_dim")

# reconcile 模式最多拍数，防「每拍都恰好满窗口」时无界循环；默认 200 拍 × 500 = 10 万人。
_DEFAULT_MAX_ROUNDS = int(os.getenv("LKM_USER_DIM_RECONCILE_MAX_ROUNDS", "200"))
_DEFAULT_WINDOW = 500

# reconcile 模式的收敛判据要用 periodic 入口真实批大小，而它是 auth 侧固定的
# RECONCILE_WINDOW（auth/user_dim_sync.py:64 = 500）；auth 内部模块对 app 不可见
# （import-linter 边界合同），只能在此镜像同一常量。改 auth 侧窗口时须同步此处。
_RECONCILE_BATCH = 500


def _flow_span(traceparent: str) -> Any:
    """把 flow 执行挂到触发方 trace（跨进程续链，fail-open）。

    tracing 未启用时 ``extract_context`` 返回 None、``tracer`` 返回 no-op tracer，
    上下文管理器照常工作，不引入额外分支。
    """
    from app.core.tracing import extract_context, tracer

    ctx = extract_context({"traceparent": traceparent}) if traceparent else None
    return tracer("lkm.flows").start_as_current_span("prefect.user_dim", context=ctx)


async def _in_session(
    fn: Callable[[Any, Any], Awaitable[int]],
) -> int:
    """经 ``auth.seams.open_session_pair`` 开跨 realm 双会话执行并提交目标会话。

    与 ``refresh_user_dim_event`` 同一范式：源会话只读、目标会话 commit/rollback/close，
    不复制任何 SQL。测试仍可 monkeypatch ``auth.user_dim_sync._session_factory`` 走融合
    schema——seam 包装是惰性取属性的，patch 照常生效。
    """
    from auth.seams import open_session_pair

    source_db, target_db = await open_session_pair()
    try:
        n = await fn(source_db, target_db)
        await target_db.commit()
        return n
    except Exception:
        await target_db.rollback()
        raise
    finally:
        await source_db.close()
        await target_db.close()


async def _reconcile_once() -> int:
    """一拍周期对账：复用 periodic 入口（自开会话 + Redis 锁 + commit），幂等。"""
    from auth.seams import reconcile_user_dim_periodic

    return await reconcile_user_dim_periodic()


async def _incremental(*, window: int) -> int:
    """单拍增量对账：一条 auth 轻扫 + 一条业务轻扫 + 差集批量 upsert（命令数恒 4/2）。"""
    from auth.seams import reconcile_user_dim_incremental

    return await _in_session(
        lambda src, tgt: reconcile_user_dim_incremental(src, tgt, window=window)
    )


async def _sync_ids(*, user_ids: list[int]) -> int:
    """显式 id 批量回填：复用 sync_dim_for_ids（命令数恒 2），空列表 no-op。"""
    from auth.seams import sync_dim_for_ids

    return await _in_session(lambda src, tgt: sync_dim_for_ids(src, tgt, user_ids))


# Prefect task 包装：生产经 task 执行以获得失败重试与运行状态；纯函数体可被编排层
# 注入替换（测试用普通函数，避免触碰 Prefect engine / 起临时 server）。
@task(name="user-dim-reconcile-once", retries=3, retry_delay_seconds=30)
async def reconcile_once_task() -> int:
    return await _reconcile_once()


@task(name="user-dim-incremental", retries=3, retry_delay_seconds=30)
async def incremental_task(*, window: int) -> int:
    return await _incremental(window=window)


@task(name="user-dim-sync-ids", retries=3, retry_delay_seconds=30)
async def sync_ids_task(*, user_ids: list[int]) -> int:
    return await _sync_ids(user_ids=user_ids)


async def orchestrate_user_dim(
    *,
    mode: str,
    window: int,
    user_ids: list[int] | None,
    max_rounds: int,
    reconcile_once: Callable[..., Awaitable[int]],
    incremental: Callable[..., Awaitable[int]],
    sync_ids: Callable[..., Awaitable[int]],
) -> dict[str, Any]:
    """纯编排控制流（不依赖 Prefect）：mode 分派与 reconcile 收敛循环。

    生产注入 Prefect task（带重试），测试注入普通函数——两条路径共用同一控制流。
    """
    if mode == "ids":
        ids = list(user_ids or [])
        updated = await sync_ids(user_ids=ids)
        return {"mode": mode, "updated": updated, "requested": len(ids)}
    if mode == "incremental":
        updated = await incremental(window=window)
        return {"mode": mode, "updated": updated, "window": window}
    if mode != "reconcile":
        raise ValueError(f"unknown mode: {mode!r}")

    total = 0
    rounds = 0
    while rounds < max_rounds:
        n = await reconcile_once()
        total += n
        rounds += 1
        # 收敛阈值必须用 periodic 入口的真实批大小，不能用 flow 入参 window：
        # _reconcile_once → reconcile_user_dim_periodic 内部固定按 RECONCILE_WINDOW
        # 取批（auth/user_dim_sync.py），window 传不进去。用 window 会在 window>500
        # 时「每拍补行恒 < window」→ 第一拍就判收敛，静默漏补；window<500 时
        # 永远判不收敛 → 跑满 max_rounds 空转。
        if n < _RECONCILE_BATCH:
            break
    return {"mode": mode, "updated": total, "rounds": rounds}


@flow(name="user-dim-reconcile", retries=1, retry_delay_seconds=60)
async def user_dim_reconcile_flow(
    *,
    mode: str = "reconcile",
    window: int = _DEFAULT_WINDOW,
    user_ids: list[int] | None = None,
    traceparent: str = "",
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    """user_dim 对账 / 回填 flow（生产入口，task 带重试）。

    - ``mode=reconcile``：循环对账至单拍补行 < window（收敛）或达 max_rounds；
    - ``mode=incremental``：单拍；
    - ``mode=ids``：对 ``user_ids`` 批量 upsert（回填）。
    """
    with _flow_span(traceparent):
        return await orchestrate_user_dim(
            mode=mode,
            window=window,
            user_ids=user_ids,
            max_rounds=max_rounds,
            reconcile_once=reconcile_once_task,
            incremental=incremental_task,
            sync_ids=sync_ids_task,
        )


def _parse_ids(raw: str) -> list[int]:
    """解析 ``--ids`` 逗号分隔的 user id；非法 token 由 argparse 报用法错误。

    原先裸 ``int()`` 对 ``--ids 1,abc`` 抛未捕获的 ValueError + 裸栈，
    运维分不清是参数写错还是回填本身失败。
    """
    ids: list[int] = []
    for token in raw.replace(" ", "").split(","):
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid user id: {token!r}") from exc
    return ids


def main() -> None:
    """CLI：运维回填 / 手动重跑（参数经 Settings/环境，不裸传凭据）。"""
    parser = argparse.ArgumentParser(description="user_dim Prefect flow 回填入口")
    parser.add_argument(
        "--mode",
        choices=["reconcile", "incremental", "ids"],
        default="incremental",
    )
    parser.add_argument("--backfill", action="store_true", help="等价 --mode ids")
    parser.add_argument("--ids", default="", help="逗号分隔 user id（回填用）")
    parser.add_argument("--window", type=int, default=_DEFAULT_WINDOW)
    parser.add_argument("--traceparent", default="", help="可选：续接父 trace")
    args = parser.parse_args()

    ids = _parse_ids(args.ids)
    mode = "ids" if args.backfill or args.ids else args.mode
    result = asyncio.run(
        user_dim_reconcile_flow(
            mode=mode,
            window=args.window,
            user_ids=ids or None,
            traceparent=args.traceparent,
        )
    )
    logger.info("user_dim flow 完成: %s", result)


if __name__ == "__main__":
    main()
