"""files 域的仓储子类：``library_files`` 的分页、行锁与内容哈希引用计数。

内容寻址去重的元数据落库（``ref_count`` 对账、审核行锁）原先散在 service 的
SQLAlchemy 表达式里，此处统一收口；service 只保留存储后端与 HTTP 形态的编排。

模型无 ``deleted_at`` 列，故基类的软删过滤天然零副作用（生命周期状态由 ``status``
列表达，``FileStatus.DELETED`` 是业务态而非 ORM 软删，故此处一律按 status 显式判定）。
"""

from __future__ import annotations

import datetime
import hashlib
import uuid

from sqlalchemy import delete, func, select, tuple_, update

from app.modules.files.errors import FileErr
from app.modules.files.models import FileStatus, LibraryFile, UploadSession
from core.db.repository import AsyncRepository
from core.err import BizError


class UploadSessionRepository(AsyncRepository[UploadSession]):
    """预签名直传会话（``upload_sessions``）的读写面。

    认领用 ``DELETE RETURNING`` 单条语句原子消费：并发下只有一方拿到行。
    会话不再依赖 Redis 存活。
    """

    model = UploadSession
    pk_attr = "upload_id"

    async def claim(
        self, upload_id: str, *, uploader_id: uuid.UUID | None = None
    ) -> UploadSession | None:
        """单条 ``DELETE RETURNING`` 原子认领；HTTP 确认同时限定归属者。"""
        stmt = delete(UploadSession).where(UploadSession.upload_id == upload_id)
        if uploader_id is not None:
            stmt = stmt.where(UploadSession.uploader_id == uploader_id)
        result = await self.db.execute(stmt.returning(UploadSession))
        return result.scalar_one_or_none()

    async def list_expired(
        self,
        *,
        before: datetime.datetime,
        after: tuple[datetime.datetime, str] | None = None,
        limit: int = 500,
    ) -> list[UploadSession]:
        """按游标锁定一批过期会话，防止清理与确认登记并发。"""
        stmt = (
            select(UploadSession)
            .where(UploadSession.created_at < before)
            .order_by(UploadSession.created_at, UploadSession.upload_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(UploadSession.created_at, UploadSession.upload_id) > after
            )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())


class LibraryFileRepository(AsyncRepository[LibraryFile]):
    model = LibraryFile

    async def lock_hash(self, sha3_hash: str) -> None:
        """持有同内容哈希的 PG 事务锁，直到最外层事务 commit/rollback。

        以域名前缀隔离其他 advisory lock；不依赖 Redis 租约，也不会在锁不可用时放行。
        """
        lock_id = int.from_bytes(
            hashlib.blake2b(f"files:{sha3_hash}".encode(), digest_size=8).digest(),
            byteorder="big",
            signed=True,
        )
        await self.db.execute(select(func.pg_advisory_xact_lock(lock_id)))

    async def increment_view(self, file_id: uuid.UUID) -> int:
        return await self._increment_counter(file_id, "view_count")

    async def increment_download(self, file_id: uuid.UUID) -> int:
        return await self._increment_counter(file_id, "download_count")

    async def _increment_counter(self, file_id: uuid.UUID, name: str) -> int:
        column = getattr(LibraryFile, name)
        stmt = (
            update(LibraryFile)
            .where(LibraryFile.id == file_id)
            .values({name: column + 1})
            .returning(column)
        )
        count = (await self.db.execute(stmt)).scalar_one_or_none()
        if count is None:
            raise BizError(FileErr.NOT_FOUND)
        return count

    @staticmethod
    def _page_conditions(category_id: str | None, status: str | None) -> list[object]:
        conditions: list[object] = []
        if category_id:
            conditions.append(LibraryFile.category_id == category_id)
        if status:
            conditions.append(LibraryFile.status == status)
        return conditions

    async def count_page(
        self, *, category_id: str | None = None, status: str | None = None
    ) -> int:
        """列表总数（与 :meth:`list_page` 同谓词）。"""
        return await self.count(*self._page_conditions(category_id, status))

    async def list_page(
        self,
        *,
        category_id: str | None = None,
        status: str | None = None,
        sort: str = "newest",
        offset: int = 0,
        limit: int = 20,
    ) -> list[LibraryFile]:
        """文件列表分页；``sort == "downloads"`` 按下载量倒序，否则按 id 倒序。"""
        # downloads 排序补 id 兜底：download_count 相同的行在 PG 里无稳定次序，
        # 不同 offset 的两次分页可能重复或漏行
        order = (
            (LibraryFile.download_count.desc(), LibraryFile.id.desc())
            if sort == "downloads"
            else LibraryFile.id.desc()
        )
        return await self.get_many(
            *self._page_conditions(category_id, status),
            order_by=order,
            offset=offset,
            limit=limit,
        )

    async def get_locked(self, file_id: uuid.UUID) -> LibraryFile | None:
        """按主键取行并加 ``FOR UPDATE`` 行锁（并发审核串行化「读 PENDING → 改 status」）。"""
        stmt = (
            select(LibraryFile)
            .where(LibraryFile.id == file_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def list_by_hash(self, sha3_hash: str) -> list[LibraryFile]:
        """引用同一物理文件（同 ``sha3_hash``）的全部条目。"""
        return await self.get_many(LibraryFile.sha3_hash == sha3_hash)

    async def count_live_by_hash(self, sha3_hash: str) -> int:
        """仍需要物理对象的条目数；被拒绝或删除的条目不算引用。"""
        return await self.count(
            LibraryFile.sha3_hash == sha3_hash,
            LibraryFile.status.in_((FileStatus.PENDING, FileStatus.APPROVED)),
        )

    async def sync_ref_count(self, sha3_hash: str) -> None:
        """把**存活引用数**写回该哈希对应的所有条目，保证 ref_count 列不漂移。

        用 count_live_by_hash（仅待审/已通过）而非全部行数：物理删除的判据就是
        「存活引用归零」（delete_file 里同源），若这里算上已拒绝/已删除行，blob 已删而
        ref_count 仍 >0，与 models.py 里「ref_count 归零时清理磁盘文件」的契约矛盾。
        """
        if not sha3_hash:
            return
        count = await self.count_live_by_hash(sha3_hash)
        await self.db.execute(
            update(LibraryFile)
            .where(LibraryFile.sha3_hash == sha3_hash)
            .values(ref_count=count)
            .execution_options(synchronize_session="fetch")
        )
