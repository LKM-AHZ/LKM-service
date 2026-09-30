"""files 模块队列任务：直传对象登记（notify 队列）与孤儿随机 key 清扫（jobs 队列）。

- ``notify_upload``：消费 ``event.notify_upload``，把已直传对象登记为 PENDING。
  入口是 MinIO/S3 桶通知回调 webhook（modules/files/notify.py）：回调只入队并立刻 200，
  真正登记由本任务异步完成，复用 ``_register_from_upload``。会话认领（删行）幂等。
- ``cleanup_expired_uploads``：消费 cron.cleanup（scheduler 每小时整点发布），
  清扫过期未确认的直传会话及其随机对象。

直传会话存 ``upload_sessions`` 表，不再依赖 Redis 存活（见
``app/modules/files/models.py::UploadSession``）。

两任务分属不同订阅（notify / jobs），各自经 ``register_task`` 注册到订阅名。
"""

import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from app.modules.files.models import UploadSession
from app.modules.files.repository import UploadSessionRepository
from app.modules.files.service import (
    _UPLOAD_TTL,
    _get_storage,
    _register_from_upload,
)
from app.modules.files.thumbnails import generate_variants_for_library_file
from app.ws.broker import publish_upload_bound
from core.db.session import new_worker_session as new_session
from core.messaging import RKEY_CLEANUP, SUB_JOBS, SUB_NOTIFY
from core.task_registry import register_cron_job, register_task

logger = logging.getLogger(__name__)


async def notify_upload(upload_id: str) -> None:
    """任务：登记直传上传。

    流程：认领会话（删行，幂等）→ 解析 meta → ``_register_from_upload`` 登记 PENDING。
    会话缺失 = 已登记或已被清扫，静默返回（幂等 soft-return，不触发死信）；
    认领到而行登记抛错 → 先把会话行写回（保留 created_at），再向上抛进死信 DLQ
    （登记未完成，避免会话缺失被当成「已登记」而丢失本次上传）。
    """
    db = await new_session()
    try:
        repo = UploadSessionRepository(db)
        session = await repo.claim(upload_id)
        if session is None:
            return  # 已登记 or 已被清扫：幂等 no-op，会话本就缺失 → 无需恢复
        try:
            meta = json.loads(session.meta)
        except json.JSONDecodeError:
            raise ValueError(f"upload session corrupt: upload_id={upload_id}") from None

        storage = _get_storage()
        try:
            reg = await _register_from_upload(
                db, meta, uuid.UUID(meta["uploader_id"]), storage
            )
            await db.commit()
        except Exception:
            # 登记失败：认领已把行删掉，写回（保留原始 created_at）后重抛进死信。
            # 恢复本身尽力而为，不覆盖原始异常（suppress 保证不 double-fail）。
            with suppress(Exception):
                await repo.restore(session)
                await db.commit()
            raise
        # 缩图（蓝图 §6.3）：登记完成、对象已在内容寻址 key 上，此时生成规格图。
        # fail-open 由 thumbnails 内部收口——缩图失败绝不影响「上传登记成功」这一语义，
        # 否则一次转码异常会把用户刚传的图连同登记一起丢掉。
        if reg is not None:
            await generate_variants_for_library_file(db, reg.id, storage)
        # 登记成功后广播给 uploader 的 WebSocket(仅成功路径；失败走上方恢复会话+重试)。
        # 广播自身 fail-open(见 broker),异常被吞,不影响任务成功语义。
        await publish_upload_bound(
            uuid.UUID(meta["uploader_id"]),
            {
                "event": "upload_registered",
                "upload_id": upload_id,
                # 登记结果作为附带数据(broadcast 失败不影响主流程)；理论非 None，
                # 防御性保留 None 以兼容 mock/异常路径。
                "file": reg.model_dump(mode="json") if reg is not None else None,
            },
        )
    finally:
        await db.close()


async def cleanup_expired_uploads() -> None:
    """周期任务：回收过期（``created_at`` 早于 ``_UPLOAD_TTL`` 窗口）的直传会话及其随机对象。

    会话在 ``upload_sessions`` 表里，本任务不再依赖 Redis。逐条「先删对象、成功再删行」：
    删对象失败就保留行留给下一轮，避免「行先没、对象永久成孤儿」——这正是原实现靠写回
    标记规避的坑，这里用同样的先后顺序保证。
    """
    storage = _get_storage()
    cutoff = datetime.now(UTC) - timedelta(seconds=_UPLOAD_TTL)
    db = await new_session()
    try:
        repo = UploadSessionRepository(db)
        expired = await repo.list_expired(before=cutoff)
        for session in expired:
            try:
                await storage.delete(session.storage_key)
            except Exception:
                # 对象删除失败：保留行留待下一轮重试，否则行先没、对象永久成孤儿
                logger.warning(
                    "cleanup storage delete failed key=%s",
                    session.storage_key,
                    exc_info=True,
                )
                continue
            # 删行即认领：并发下 confirm/notify 可能已删掉它，影响行数为 0 是正常竞态
            await repo.hard_delete_where(UploadSession.upload_id == session.upload_id)
        await db.commit()
    finally:
        await db.close()


register_task(SUB_NOTIFY.name, "notify_upload", notify_upload)
register_task(SUB_JOBS.name, "cleanup_expired_uploads", cleanup_expired_uploads)
register_cron_job(
    job_id="cleanup_expired_uploads",
    cron="0 * * * *",  # 每小时整点
    routing_key=RKEY_CLEANUP,
    fn="cleanup_expired_uploads",
)
