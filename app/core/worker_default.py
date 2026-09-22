"""compose worker 服务入口：跑默认队列 worker。"""

import asyncio
import logging

from app.core.logging import setup_logging
from app.core.worker import run_default_worker

logger = logging.getLogger("lkm.worker_default")


async def _main() -> None:
    # worker 进程不经 ASGI 入口，全仓只有 app/main.py 调 setup_logging → 此前这里根 logger
    # 没挂 handler、级别停在 WARNING：消费链路的 INFO（如「幂等跳过已处理」）在容器里全部
    # 看不到，WARNING+ 也走 lastResort 的纯文本，与 API 侧 JSON 日志无法对齐 trace
    setup_logging()
    try:
        await run_default_worker()
    except Exception:
        # 崩溃要留下结构化日志（含 trace_id），而不是裸 traceback 退出——便于编排层告警
        logger.exception("默认 worker 异常退出")
        raise


if __name__ == "__main__":
    asyncio.run(_main())
