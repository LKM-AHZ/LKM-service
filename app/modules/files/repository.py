"""files 域的仓储子类：``library_files`` 的分页、行锁与内容哈希引用计数。

内容寻址去重的元数据落库（``ref_count`` 对账、审核行锁）原先散在 service 的
SQLAlchemy 表达式里，此处统一收口；service 只保留存储后端与 HTTP 形态的编排。

模型无 ``deleted_at`` 列，故基类的软删过滤天然零副作用（生命周期状态由 ``status``
列表达，``FileStatus.DELETED`` 是业务态而非 ORM 软删，故此处一律按 status 显式判定）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select

from app.modules.files.models import FileStatus, LibraryFile, UploadSession
from core.db.repository import AsyncRepository


class UploadSessionRepository(AsyncRepository[UploadSession]):
    """预签名直传会话（``upload_sessions``）的读写面。

    认领用「DELETE 影响行数」判定归属 —— 等价原先 Redis ``GETDEL`` 的原子消费：并发下只有
    一方拿到行，另一方拿到 0 行即视为已过期/已用。会话不再依赖 Redis 存活。
    """

    model = UploadSession
    pk_attr = "upload_id"

    async def claim(self, upload_id: str) -> UploadSession | None:
        """原子认领一个会话：返回被删掉的行（含 meta）；不存在/已被认领 → None。"""
        row = await self.get(upload_id)
        if row is None:
            return None
        deleted = await self.hard_delete_where(UploadSession.upload_id == upload_id)
        if deleted == 0:
            return None
        return row

    async def restore(self, row: UploadSession) -> None:
        """把已认领但登记失败的行写回（保留原 ``created_at``，不被立即判龄清扫）。"""
        await self.pg_upsert(
            {
                "upload_id": row.upload_id,
                "uploader_id": row.uploader_id,
                "storage_key": row.storage_key,
                "meta": row.meta,
                "created_at": row.created_at,
            },
            index_elements=["upload_id"],
            update_columns=["uploader_id", "storage_key", "meta", "created_at"],
        )

    async def list_expired(self, *, before: datetime.datetime) -> list[UploadSession]:
        """``created_at`` 早于 ``before`` 的会话（供孤儿清扫）。"""
        result = await self.db.execute(
            select(UploadSession).where(UploadSession.created_at < before)
        )
        return list(result.scalars().all())


class LibraryFileRepository(AsyncRepository[LibraryFile]):
    model = LibraryFile

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
        stmt = select(LibraryFile).where(LibraryFile.id == file_id).with_for_update()
        return (await self.db.execute(stmt)).scalars().first()

    async def list_by_hash(self, sha3_hash: str) -> list[LibraryFile]:
        """引用同一物理文件（同 ``sha3_hash``）的全部条目。"""
        return await self.get_many(LibraryFile.sha3_hash == sha3_hash)

    async def count_by_hash(self, sha3_hash: str) -> int:
        """引用该物理文件的条目数（DB 聚合，含已标 DELETED 的条目）。"""
        return await self.count(LibraryFile.sha3_hash == sha3_hash)

    async def count_live_by_hash(self, sha3_hash: str) -> int:
        """同一内容哈希下未标 ``DELETED`` 的条目数（末引用物理删除决策用）。"""
        return await self.count(
            LibraryFile.sha3_hash == sha3_hash,
            LibraryFile.status != FileStatus.DELETED,
        )

    async def sync_ref_count(self, sha3_hash: str) -> None:
        """把**存活引用数**写回该哈希对应的所有条目，保证 ref_count 列不漂移。

        用 count_live_by_hash（不含 DELETED）而非全部行数：物理删除的判据就是
        「存活引用归零」（delete_file 里同源），若这里算上 DELETED 行，blob 已删而
        ref_count 仍 >0，与 models.py 里「ref_count 归零时清理磁盘文件」的契约矛盾。
        """
        if not sha3_hash:
            return
        count = await self.count_live_by_hash(sha3_hash)
        for row in await self.list_by_hash(sha3_hash):
            row.ref_count = count
