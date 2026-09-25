"""files 模块队列任务：直传对象登记（notify 队列）与孤儿随机 key 清扫（jobs 队列）。

- ``notify_upload``：消费 ``event.notify_upload``，把已直传对象登记为 PENDING。
  入口是 MinIO/S3 桶通知回调 webhook（modules/files/notify.py）：回调只入队并立刻 200，
  真正登记由本任务异步完成，复用 ``_register_from_upload``。Redis getdel 幂等。
- ``cleanup_expired_uploads``：消费 cron.cleanup（scheduler 每小时整点发布），
  清扫过期未确认的直传随机 key 及标记。

两任务分属不同订阅（notify / jobs），各自经 ``register_task`` 注册到订阅名。
"""

import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from app.core.messaging import RKEY_CLEANUP, SUB_JOBS, SUB_NOTIFY
from app.core.redis import get_redis
from app.core.task_registry import register_cron_job, register_task
from app.db.session import new_worker_session as new_session
from app.modules.files.service import (
    _UPLOAD_TTL,
    _get_storage,
    _register_from_upload,
    _upload_key,
)
from app.modules.files.thumbnails import generate_variants_for_library_file
from app.ws.broker import publish_upload_bound

logger = logging.getLogger(__name__)

_MATCH = "upload:*"


async def notify_upload(upload_id: str) -> None:
    """任务：登记直传上传。

    流程：GETDEL 标记（幂等）→ 解析 meta → ``_register_from_upload`` 登记 PENDING。
    标记缺失 = 已登记或已被清扫，静默返回（幂等 soft-return，不触发死信）；
    标记在而登记抛错 → 先按原值恢复标记（保 created_at），再向上抛进死信 DLQ
    （登记未完成，死信后由前端/重投流程处理，避免标记缺失被测为已登记而丢失）。
    """
    redis = await get_redis()
    if redis is None:
        # Redis 未启用：没有标记可读，无从登记（fail-close：无 Redis 即无直传流程）。
        return
    meta_raw = await redis.getdel(_upload_key(upload_id))
    if not meta_raw:
        return  # 已登记 or 已被清扫：幂等 no-op，标记本就缺失 → 无需恢复
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError:
        raise ValueError(f"upload marker corrupt: upload_id={upload_id}") from None

    db = await new_session()
    try:
        storage = _get_storage()
        reg = await _register_from_upload(
            db, meta, uuid.UUID(meta["uploader_id"]), storage
        )
        await db.commit()
        # 缩图（蓝图 §6.3）：登记完成、对象已在内容寻址 key 上，此时生成规格图。
        # fail-open 由 thumbnails 内部收口——缩图失败绝不影响「上传登记成功」这一语义，
        # 否则一次转码异常会把用户刚传的图连同登记一起丢掉。
        if reg is not None:
            await generate_variants_for_library_file(db, reg.id, storage)
        # 登记成功后广播给 uploader 的 WebSocket(仅成功路径；失败走下方恢复标记+重试)。
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
    except Exception:
        # 登记失败（存储/DB 错误）：GETDEL 已把标记取走，若不恢复，后续处理会因标记缺失
        # 被当成幂等 no-op 而静默吞掉本次上传。恢复原标记（保留原始 created_at，孤儿
        # 清扫年龄逻辑不变）后重新抛出进死信。恢复本身尽力而为：
        # redis 恰好不可用也不覆盖原始异常（suppress 保证不 double-fail）。
        with suppress(Exception):
            await redis.set(_upload_key(upload_id), meta_raw)
        raise
    finally:
        await db.close()


def _parse_created_at(meta_raw: str) -> datetime | None:
    """从标记 JSON 解析 ``created_at``；标记残缺/坏 JSON 视为不可判龄（返回 None）。"""
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError:
        return None
    # 合法但非 dict 的 JSON（null/[]/123）无 created_at 可言 → 同样视为不可判龄
    if not isinstance(meta, dict):
        return None
    raw = meta.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # 无时区的遗留标记直接与 aware 的 now 相减会抛 TypeError，冒泡会中断整轮清扫；
    # 按 UTC 归一，保持"不可判龄才跳过"的既有语义
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def cleanup_expired_uploads() -> None:
    """周期任务：清掉已过期(created_at 早于 _UPLOAD_TTL 窗口)但未确认的直传随机 key 及标记。

    Redis 未启用（get_redis 返回 None）直接 return，不报错。
    """
    redis = await get_redis()
    if redis is None:
        return
    storage = _get_storage()
    now = datetime.now(UTC)
    # redis.asyncio 的 scan_iter 是异步迭代器，直接 async for 消费
    async for marker in redis.scan_iter(match=_MATCH):
        # GETDEL 原子认领：若 notify_upload/confirm_upload 刚认领同一标记并正在搬对象，
        # 这里拿到 None 直接跳过，避免把在途登记赖以读取的随机对象删掉。
        meta_raw = await redis.getdel(marker)
        if meta_raw is None:
            continue  # 已被他人认领/清扫，跳过
        created_at = _parse_created_at(meta_raw)
        # 年龄窗口按"创建后至少 _UPLOAD_TTL 秒"判定过期；created_at 缺失/坏 JSON →
        # 保守不删（无法判龄），避免误删仍在直传中的标记。
        if created_at is None or (now - created_at).total_seconds() <= _UPLOAD_TTL:
            with suppress(Exception):
                await redis.set(marker, meta_raw)  # 未过期/不可判龄：尽力写回
            continue
        # 过期：先删对应随机 key 对象（key 存在时），删成功才删标记
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        key = meta.get("key")
        if isinstance(key, str) and key:
            try:
                await storage.delete(key)
            except Exception:
                # 对象删除失败：写回标记留给下一轮重试，否则标记先没、对象永久成孤儿
                logger.warning("cleanup storage delete failed key=%s", key, exc_info=True)
                with suppress(Exception):
                    await redis.set(marker, meta_raw)
                continue


register_task(SUB_NOTIFY.name, "notify_upload", notify_upload)
register_task(SUB_JOBS.name, "cleanup_expired_uploads", cleanup_expired_uploads)
register_cron_job(
    job_id="cleanup_expired_uploads",
    cron="0 * * * *",  # 每小时整点
    routing_key=RKEY_CLEANUP,
    fn="cleanup_expired_uploads",
)
