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
_CLEANUP_BATCH = 500


async def notify_upload(upload_id: str) -> None:
    """任务：登记直传上传。

    流程：认领会话（删行，幂等）→ 解析 meta → ``_register_from_upload`` 登记 PENDING。
    会话缺失 = 已登记或已被清扫，静默返回（幂等 soft-return，不触发死信）；
    认领与登记置于同一 savepoint；登记失败回滚后原会话仍可被重试。
    """
    db = await new_session()
    try:
        repo = UploadSessionRepository(db)
        try:
            async with db.begin_nested():
                session = await repo.claim(upload_id)
                if session is None:
                    return  # 已登记 or 已被清扫：幂等 no-op
                try:
                    meta = json.loads(session.meta)
                except json.JSONDecodeError:
                    raise ValueError(
                        f"upload session corrupt: upload_id={upload_id}"
                    ) from None
                storage = _get_storage()
                reg = await _register_from_upload(
                    db, meta, session.uploader_id, storage
                )
            await db.commit()
        except Exception:
            # savepoint 已恢复 DELETE；落定外层事务，使会话保持可重试。
            with suppress(Exception):
                await db.commit()
            raise
        if reg is not None:
            await generate_variants_for_library_file(db, reg.id, storage)
        # 登记成功后广播给 uploader 的 WebSocket(仅成功路径；失败走上方恢复会话+重试)。
        # 广播自身 fail-open(见 broker),异常被吞,不影响任务成功语义。
        await publish_upload_bound(
            session.uploader_id,
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

    会话在 ``upload_sessions`` 表里，本任务不再依赖 Redis。先锁定一批会话，再逐条
    「先删对象、成功再删行」：
    删对象失败就保留行留给下一轮，避免「行先没、对象永久成孤儿」——这正是原实现靠写回
    标记规避的坑，这里用同样的先后顺序保证。
    """
    storage = _get_storage()
    cutoff = datetime.now(UTC) - timedelta(seconds=_UPLOAD_TTL)
    db = await new_session()
    try:
        repo = UploadSessionRepository(db)
        cursor: tuple[datetime, str] | None = None
        while True:
            expired = await repo.list_expired(
                before=cutoff, after=cursor, limit=_CLEANUP_BATCH
            )
            if not expired:
                break
            for session in expired:
                try:
                    await storage.delete(session.storage_key)
                except Exception:
                    logger.warning(
                        "cleanup storage delete failed key=%s",
                        session.storage_key,
                        exc_info=True,
                    )
                    continue
                await repo.hard_delete_where(
                    UploadSession.upload_id == session.upload_id
                )
            cursor = expired[-1].created_at, expired[-1].upload_id
            await db.commit()
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
