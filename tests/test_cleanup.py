"""孤儿直传会话清扫 cron 任务测试：按 created_at 判过期、删过期会话及其随机对象，
未过期保留；对象删除失败时保留会话行留给下一轮。

会话存 ``upload_sessions`` 表（不再依赖 Redis 存活，见 models.UploadSession）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files import tasks as cleanup
from app.modules.files.models import UploadSession
from app.modules.files.service import _UPLOAD_TTL


class _FakeStorage:
    def __init__(self, deleted: list[str], fail_on: set[str] | None = None) -> None:
        self.deleted = deleted
        self.fail_on = fail_on or set()

    async def delete(self, key: str) -> None:
        if key in self.fail_on:
            raise RuntimeError("storage boom")
        self.deleted.append(key)


def _new_session_for(db: AsyncSession):
    """把任务内部的 ``new_session`` 指到测试会话（与 test_notify 同款）。"""

    async def _new_session() -> AsyncSession:
        return db

    return _new_session


async def _add_session(
    db: AsyncSession, upload_id: str, key: str, *, age_seconds: int
) -> None:
    db.add(
        UploadSession(
            upload_id=upload_id,
            uploader_id=uuid.uuid4(),
            storage_key=key,
            meta="{}",
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
    )
    await db.flush()


async def _remaining(db: AsyncSession) -> set[str]:
    rows = await db.execute(select(UploadSession.upload_id))
    return set(rows.scalars().all())


async def test_cleanup_deletes_expired_upload(
    db: AsyncSession, monkeypatch: Any
) -> None:
    """过期会话(created_at 早于窗口) → 删随机对象 + 删行；未过期(年轻)保留。"""
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "_get_storage", lambda: _FakeStorage(deleted))
    monkeypatch.setattr(cleanup, "new_session", _new_session_for(db))
    await _add_session(db, "aaa", "up/aaa", age_seconds=_UPLOAD_TTL + 10)  # 过期
    await _add_session(db, "bbb", "up/bbb", age_seconds=10)  # 年轻

    await cleanup.cleanup_expired_uploads()

    assert deleted == ["up/aaa"]
    assert await _remaining(db) == {"bbb"}


async def test_cleanup_keeps_fresh_uploads(db: AsyncSession, monkeypatch: Any) -> None:
    """所有会话都未过期(年龄<=窗口) → 全保留，不删任何对象。"""
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "_get_storage", lambda: _FakeStorage(deleted))
    monkeypatch.setattr(cleanup, "new_session", _new_session_for(db))
    await _add_session(db, "aaa", "up/aaa", age_seconds=_UPLOAD_TTL - 5)
    await _add_session(db, "bbb", "up/bbb", age_seconds=_UPLOAD_TTL // 2)

    await cleanup.cleanup_expired_uploads()

    assert deleted == []
    assert await _remaining(db) == {"aaa", "bbb"}


async def test_cleanup_keeps_session_when_storage_delete_fails(
    db: AsyncSession, monkeypatch: Any
) -> None:
    """对象删除失败 → 保留会话行留给下一轮重试。

    这是原实现用「写回标记」规避的坑（行先没、对象永久成孤儿），本实现用同样的先后顺序
    保证：先删对象成功、才删行。
    """
    deleted: list[str] = []
    monkeypatch.setattr(
        cleanup, "_get_storage", lambda: _FakeStorage(deleted, fail_on={"up/aaa"})
    )
    monkeypatch.setattr(cleanup, "new_session", _new_session_for(db))
    await _add_session(db, "aaa", "up/aaa", age_seconds=_UPLOAD_TTL + 10)

    await cleanup.cleanup_expired_uploads()

    assert deleted == []  # 删除失败，未计入
    assert await _remaining(db) == {"aaa"}  # 行保留待下轮


async def test_cleanup_continues_past_failed_batch(
    db: AsyncSession, monkeypatch: Any
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "_CLEANUP_BATCH", 1)
    monkeypatch.setattr(
        cleanup, "_get_storage", lambda: _FakeStorage(deleted, fail_on={"up/aaa"})
    )
    monkeypatch.setattr(cleanup, "new_session", _new_session_for(db))
    for upload_id in ("aaa", "bbb", "ccc"):
        await _add_session(
            db, upload_id, f"up/{upload_id}", age_seconds=_UPLOAD_TTL + 10
        )

    await cleanup.cleanup_expired_uploads()

    assert deleted == ["up/bbb", "up/ccc"]
    assert await _remaining(db) == {"aaa"}
