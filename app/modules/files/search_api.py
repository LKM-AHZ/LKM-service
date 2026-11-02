"""files 域的检索投影：索引文档、FTS 查询分支与可见性口径。

search 模块不再读 ``files.models``：公开可见（APPROVED + public + 非旧版本）的口径由
files 自己给出。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from app.contracts.search import IndexDoc
from app.modules.files.models import FileStatus, LibraryFile
from core.common import parse_tags
from core.ports.snapshot import get_user_display_names, get_user_snapshot_batch

_LIKE_ESCAPE = "\\"


def _like_pattern(term: str) -> str:
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _tag_list(raw: str | None) -> list[str]:
    return parse_tags(raw) if raw else []


def build_file_doc(item: LibraryFile, uploader_name: str = "") -> IndexDoc:
    """已审核公开文件的正文/元数据索引文档。"""
    return {
        "id": str(item.id),
        "content_type": "library_file",
        "board_id": "",
        "title": item.original_name,
        "excerpt": item.description,
        "content": item.extracted_text,
        "summary": item.document_code or "",
        "tags": _tag_list(item.tags),
        "keywords": [item.category_id, item.document_code or ""],
        "author_id": str(item.uploader_id),
        "author_name": uploader_name,
        "created_at": item.created_at.isoformat(),
    }


def _newer_approved_exists() -> Any:
    """「同 document_code 存在更高版本的已审核文件」谓词。"""
    newer = aliased(LibraryFile)
    return (
        select(newer.id)
        .where(
            newer.document_code == LibraryFile.document_code,
            newer.version > LibraryFile.version,
            newer.status == FileStatus.APPROVED,
        )
        .exists()
    )


def search_select(term: str) -> Select[Any]:
    """文件侧的检索分支：``(id, source, rank, created_at)``，供 search 做 UNION。"""
    query = func.websearch_to_tsquery("simple", term)
    pattern = _like_pattern(term)
    file_vector = func.to_tsvector(
        "simple",
        func.concat_ws(
            " ",
            LibraryFile.original_name,
            LibraryFile.document_code,
            LibraryFile.description,
            LibraryFile.tags,
            LibraryFile.extracted_text,
        ),
    )
    file_match = or_(
        file_vector.bool_op("@@")(query),
        LibraryFile.original_name.ilike(pattern, escape=_LIKE_ESCAPE),
        LibraryFile.document_code.ilike(pattern, escape=_LIKE_ESCAPE),
        LibraryFile.description.ilike(pattern, escape=_LIKE_ESCAPE),
        LibraryFile.tags.ilike(pattern, escape=_LIKE_ESCAPE),
        LibraryFile.extracted_text.ilike(pattern, escape=_LIKE_ESCAPE),
    )
    return select(
        LibraryFile.id.label("id"),
        literal("library_file").label("source"),
        func.ts_rank(file_vector, query).label("rank"),
        LibraryFile.created_at.label("created_at"),
    ).where(
        LibraryFile.status == FileStatus.APPROVED,
        LibraryFile.classification == "public",
        ~_newer_approved_exists(),
        file_match,
    )


async def _display_name(db: AsyncSession, user_id: uuid.UUID | None) -> str:
    if user_id is None:
        return ""
    snaps = await get_user_snapshot_batch(db, user_ids=[user_id])
    snap = snaps.get(user_id)
    return snap.display_name if snap else ""


async def index_doc(db: AsyncSession, file_id: uuid.UUID) -> IndexDoc | None:
    """可索引则返回文档，否则 ``None``（未审核 / 非公开 / 已有更新版本）。"""
    row = await db.get(LibraryFile, file_id)
    if (
        row is None
        or row.status != FileStatus.APPROVED
        or row.classification != "public"
    ):
        return None
    newer = await db.scalar(
        select(LibraryFile.id)
        .where(
            LibraryFile.document_code == row.document_code,
            LibraryFile.version > row.version,
            LibraryFile.status == FileStatus.APPROVED,
        )
        .limit(1)
    )
    if newer is not None:
        return None
    return build_file_doc(row, await _display_name(db, row.uploader_id))


def select_public_by_ids(ids: list[uuid.UUID]) -> Select[Any]:
    return select(LibraryFile).where(
        LibraryFile.id.in_(ids),
        LibraryFile.status == FileStatus.APPROVED,
        LibraryFile.classification == "public",
    )


def select_public_batch(after_id: uuid.UUID, limit: int) -> Select[Any]:
    return (
        select(LibraryFile)
        .where(
            LibraryFile.id > after_id,
            LibraryFile.status == FileStatus.APPROVED,
            LibraryFile.classification == "public",
            ~_newer_approved_exists(),
        )
        .order_by(LibraryFile.id)
        .limit(limit)
    )


async def index_docs_batch(
    db: AsyncSession, *, after_id: uuid.UUID, limit: int
) -> tuple[list[IndexDoc], uuid.UUID]:
    """按 id 升序取一窗公开文件并映射成文档（全量重建索引）。"""
    rows = (await db.execute(select_public_batch(after_id, limit))).scalars().all()
    if not rows:
        return [], after_id
    names = await get_user_display_names(db, {r.uploader_id for r in rows})
    docs = [build_file_doc(r, names.get(r.uploader_id, "")) for r in rows]
    return docs, rows[-1].id
