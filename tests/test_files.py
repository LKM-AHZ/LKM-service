"""files 域行为测试（M3.B S5 拆库 dual 真 PG 迁移版）。

拆库后业务库（Base 无 users）不再有 User/Profile 表；users/profiles 迁 auth 库（AuthBase）。
文件行的 uploader_id / 展示名（uploader_name）一律走 auth realm：
- ``_au(auth_db,...)`` 在本测 auth schema 写真实 User(+Profile)，返回稳定 ``AuthUser``；
  业务行裸 int ``.id`` / 登录身份 ``.token`` 引用它在 auth realm 的值。
- service(list/get_file/create_file/review_file/delete_file/confirm…) 尾尾经
  ``_uploader_map → get_user_snapshot_batch`` 解析展示名；故凡调用 file service 的用例都须
  注入 ``auth_db``+``auth_seam_realm``——seam(替身直读本测 auth_db)跨 realm 返回 uploader_name，
  绝不落业务 db 查 User（拆库后业务 realm 无 users，直读会 UndefinedTable）。
- RBAC 权限点（files.upload/files.download）仍落业务 realm（RolePermission，符合生产）。

真双 PG(lkm / lkm_auth) schema-per-test 各建 schema即可跑绿。
"""

import asyncio
import hashlib
import io
import json
import pathlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth.models  # noqa: F401  副作用导入（auth 表挂 AuthBase 需收集）
from app.core.config import settings
from app.core.err import BizError, CommonErr
from app.modules.files.errors import FileErr
from app.modules.files.models import FileStatus, LibraryFile
from app.modules.files.schemas import DownloadUrlInfo, FileCreate, FileInfo
from app.modules.files.service import (
    bump_download,
    confirm_upload,
    create_file,
    delete_file,
    get_file,
    list_files,
    review_file,
    upload_init,
)
from app.modules.storage.s3 import S3Storage
from tests.conftest import AuthUser, auth_user_uid

# 合法的 uuid7 形态（第 3 段以 7 开头、第 4 段以 8 开头），用于"不存在"的 id 用例。
_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


# ---------------------------------------------------------------------------
# auth realm 用户 + 业务权限 helper
# ---------------------------------------------------------------------------
async def _au(
    auth_db: AsyncSession,
    username: str = "alice",
    email: str | None = None,
    nickname: str | None = None,
    account_level: str = "normal",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser(id/username/account_level/token)。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=email or f"{username}@example.com",
        nickname=nickname,
        account_level=account_level,
        role=role,
    )


async def _grant(db: AsyncSession, role_name: str, *perms: str) -> None:
    from app.modules.admin.models import RolePermission

    for p in perms:
        db.add(RolePermission(role_name=role_name, permission=p))
    await db.flush()


def _h(au: AuthUser) -> dict[str, str]:
    """AuthUser mint 的 Web Bearer → Authorization 头。"""
    return {"Authorization": f"Bearer {au.token}"}


async def _file(
    db: AsyncSession,
    auth_db: AsyncSession,
    uploader: AuthUser,
    original_name: str = "讲义.pdf",
    category_id: str = "math",
    tags: tuple[str, ...] = ("数学",),
) -> FileInfo:
    """以 uploader 建一件文件。uploader_id 引用 auth realm 的 uuid 主键。

    注：create_file 尾会经 seam(_uploader_map)解析展示名，故须 seam ON + auth user 在 auth_db。
    """
    return await create_file(
        db,
        uploader.id,
        FileCreate(
            original_name=original_name,
            mime_type="application/pdf",
            category_id=category_id,
            description="一份讲义",
            tags=list(tags),
        ),
        stream=io.BytesIO(b"%PDF-1.4 fake content"),
    )


class TestFilesService:
    async def should_create_file_with_nickname(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db, nickname="爱丽丝")

        f = await _file(db, auth_db, uploader)

        assert isinstance(f.id, uuid.UUID)
        assert f.uploader_id == uploader.id
        assert f.uploader_name == "爱丽丝"
        assert f.tags == ["数学"]
        assert f.status == "pending"
        assert f.download_count == 0
        assert f.view_count == 0
        stored = (
            (await db.execute(select(LibraryFile).where(LibraryFile.id == f.id)))
            .scalars()
            .one()
        )
        physical = await asyncio.to_thread(
            pathlib.Path(
                stored.storage_path or (tmp_path / stored.stored_name)
            ).read_bytes
        )
        assert physical == b"%PDF-1.4 fake content"

    async def should_use_username_when_no_nickname(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db, username="bob", nickname=None)

        f = await _file(db, auth_db, uploader)

        assert f.uploader_name == "bob"

    async def should_list_files_paginated(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        await _file(db, auth_db, uploader, original_name="文件一.pdf")
        await _file(db, auth_db, uploader, original_name="文件二.pdf")

        page = await list_files(db, page=1, limit=1)

        assert page.total == 2
        assert page.pages == 2
        assert len(page.items) == 1
        assert page.items[0].original_name == "文件二.pdf"

    async def should_filter_files_by_category(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        await _file(db, auth_db, uploader, category_id="math")
        await _file(
            db, auth_db, uploader, original_name="物理题.pdf", category_id="physics"
        )

        page = await list_files(db, category_id="math")

        assert page.total == 1
        assert page.items[0].category_id == "math"

    async def should_sort_by_downloads(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        hot = await _file(db, auth_db, uploader, original_name="热门.zip")
        await _file(db, auth_db, uploader, original_name="冷门.pdf")
        await bump_download(db, hot.id)
        await bump_download(db, hot.id)

        page = await list_files(db, sort="downloads")

        assert [f.original_name for f in page.items] == ["热门.zip", "冷门.pdf"]

    async def should_get_file_and_bump_view(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await _file(db, auth_db, uploader)

        first = await get_file(db, f.id, bump_view=True)
        second = await get_file(db, f.id, bump_view=True)

        assert first.view_count == 1
        assert second.view_count == 2

    async def should_reject_nonexistent_file(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            await get_file(db, _MISSING_ID)

        assert exc.value.errcode == FileErr.NOT_FOUND

    async def should_increment_download(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await _file(db, auth_db, uploader)

        assert await bump_download(db, f.id) == 1
        assert await bump_download(db, f.id) == 2

    async def should_reject_upload_over_limit(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)

        with pytest.raises(BizError) as exc:
            await create_file(
                db,
                uploader.id,
                FileCreate(original_name="超大.zip", mime_type="application/zip"),
                stream=io.BytesIO(b"x" * 10),
                max_bytes=5,
            )

        assert exc.value.errcode == FileErr.TOO_LARGE
        assert len((await db.execute(select(LibraryFile))).scalars().all()) == 0
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []

    async def should_accept_upload_at_exact_limit(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)

        f = await create_file(
            db,
            uploader.id,
            FileCreate(original_name="正好.zip", mime_type="application/zip"),
            stream=io.BytesIO(b"x" * 5),
            max_bytes=5,
        )

        assert f.size == 5


class TestFilesRoutes:
    async def _mk_user(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        username: str = "tester",
        nickname: str | None = None,
        *,
        upload: bool = False,
        download: bool = False,
    ) -> AuthUser:
        # 上传/下载/预览端点需权限点：normal:member 授 files.upload/files.download，
        # 与生产 DEFAULT_GRANTS seed 一致。
        perms = []
        if upload:
            perms.append("files.upload")
        if download:
            perms.append("files.download")
        await _grant(db, "normal:member", *perms)
        return await _au(
            auth_db, username=username, nickname=nickname, account_level="normal", role="member"
        )

    async def _upload(
        self,
        client: Any,
        uploader: AuthUser,
        original_name: str = "讲义.pdf",
        category_id: str = "math",
    ):
        return await client.post(
            "/api/v1/files",
            headers=_h(uploader),
            files={
                "file": (
                    original_name,
                    io.BytesIO(b"%PDF-1.4 content"),
                    "application/pdf",
                )
            },
            data={
                "category_id": category_id,
                "description": "测试上传",
                "tags": '["数学"]',
            },
        )

    async def should_reject_upload_without_auth(
        self,
        client: Any,
        db: AsyncSession,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        resp = await client.post(
            "/api/v1/files",
            files={"file": ("a.pdf", io.BytesIO(b"content"), "application/pdf")},
        )

        assert resp.status_code == 403
        assert resp.json()["code"] == CommonErr.FORBIDDEN

    async def should_upload_file_with_token(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._mk_user(db, auth_db, upload=True)
        resp = await self._upload(client, uploader)

        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        data = resp.json()["data"]
        assert data["uploader_id"] == str(uploader.id)
        assert data["original_name"] == "讲义.pdf"
        assert data["mime_type"] == "application/pdf"
        assert data["size"] == len(b"%PDF-1.4 content")
        assert data["status"] == "pending"
        assert data["tags"] == ["数学"]

    async def should_list_files_publicly(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._mk_user(db, auth_db, upload=True)
        await self._upload(client, uploader, original_name="公开资料.zip")

        resp = await client.get("/api/v1/files")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["original_name"] == "公开资料.zip"
        assert data["items"][0]["uploader_name"] == "tester"

    async def should_get_file_detail(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._mk_user(db, auth_db, upload=True)
        created = (await self._upload(client, uploader)).json()["data"]
        file_id = created["id"]

        resp = await client.get(f"/api/v1/files/{file_id}")

        assert resp.status_code == 200
        assert resp.json()["data"]["view_count"] == 1

    async def should_increment_download_through_api(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._mk_user(
            db, auth_db, download=True, upload=True
        )
        created = (await self._upload(client, uploader)).json()["data"]
        file_id = created["id"]

        resp = await client.post(
            f"/api/v1/files/{file_id}/download",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["download_count"] == 1

    async def should_reject_nonexistent_file_detail(
        self, client: Any, db: AsyncSession
    ):
        resp = await client.get(f"/api/v1/files/{_MISSING_ID}")

        assert resp.status_code == 404
        assert resp.json()["code"] == FileErr.NOT_FOUND

    async def should_reject_oversized_upload_through_api(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        monkeypatch.setattr(settings, "max_upload_bytes", 8)
        uploader = await self._mk_user(db, auth_db, upload=True)

        resp = await client.post(
            "/api/v1/files",
            headers=_h(uploader),
            files={"file": ("big.zip", io.BytesIO(b"x" * 100), "application/zip")},
        )

        assert resp.status_code == 413
        assert resp.json()["code"] == FileErr.TOO_LARGE
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []

    async def should_not_persist_record_when_storage_fails(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        store = tmp_path / "store"
        store.mkdir()
        (store / "blocked.txt").write_text("x")
        monkeypatch.setattr(settings, "files_store_dir", str(store / "blocked.txt" / "sub"))

        uploader = await _au(auth_db)
        with pytest.raises(BizError) as exc:
            await _file(db, auth_db, uploader)

        assert exc.value.errcode == FileErr.STORE_ERROR
        assert len((await db.execute(select(LibraryFile))).scalars().all()) == 0


class TestFilesDedupAndReview:
    async def _upload_raw(
        self,
        db: AsyncSession,
        uploader: AuthUser,
        content: bytes,
        original_name: str = "资料.pdf",
        category_id: str = "math",
    ) -> FileInfo:
        return await create_file(
            db,
            uploader.id,
            FileCreate(
                original_name=original_name,
                mime_type="application/pdf",
                category_id=category_id,
                description="",
                tags=[],
            ),
            stream=io.BytesIO(content),
        )

    @staticmethod
    def _physical_files(tmp_path: pathlib.Path) -> list[pathlib.Path]:
        # 分桶后文件在 <hash[:2]>/ 子目录，需递归查找
        return [
            p
            for p in tmp_path.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp")
        ]

    async def _list_physical(self, tmp_path: pathlib.Path) -> list[pathlib.Path]:
        return await asyncio.to_thread(self._physical_files, tmp_path)

    async def should_dedup_same_content(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        content = b"same-content-bytes"

        await self._upload_raw(db, uploader, content, original_name="a.pdf")
        await self._upload_raw(db, uploader, content, original_name="b.pdf")

        # 同一物理文件只落盘一份
        physical_files = await self._list_physical(tmp_path)
        assert len(physical_files) == 1
        # 元数据条目是两个（两次上传各一条），引用计数为 2
        rows = (await db.execute(select(LibraryFile))).scalars().all()
        assert len(rows) == 2
        assert all(r.ref_count == 2 for r in rows)
        # 两条目共享同一内容哈希（SHA3-256 恒为 64 位 16 进制）
        digest = rows[0].sha3_hash
        assert digest is not None and digest == rows[1].sha3_hash
        assert len(digest) == 64

    async def should_not_persist_physical_when_only_error(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """两次相同内容上传后删除其一，物理文件因仍被引用而保留。"""
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        content = b"shared-bytes"
        f1 = await self._upload_raw(db, uploader, content, original_name="a.pdf")
        await self._upload_raw(db, uploader, content, original_name="b.pdf")

        await delete_file(db, f1.id, actor_id=uploader.id)

        rows = (
            (await db.execute(select(LibraryFile).where(LibraryFile.id == f1.id)))
            .scalars()
            .one()
        )
        assert rows.status == "deleted"
        # 还有另一个引用，物理文件保留
        physical = await self._list_physical(tmp_path)
        assert len(physical) == 1

    async def should_purge_physical_when_last_reference_deleted(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        content = b"unique-bytes"
        f1 = await self._upload_raw(db, uploader, content, original_name="a.pdf")
        f2 = await self._upload_raw(db, uploader, content, original_name="b.pdf")

        await delete_file(db, f1.id, actor_id=uploader.id)
        await delete_file(db, f2.id, actor_id=uploader.id)

        physical = await self._list_physical(tmp_path)
        assert physical == []

    async def should_reject_non_admin_review(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await self._upload_raw(db, uploader, b"x")

        with pytest.raises(BizError) as exc:
            await review_file(db, f.id, FileStatus.APPROVED, is_admin=False)
        assert exc.value.errcode == FileErr.STORE_ERROR

    async def should_approve_file(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await self._upload_raw(db, uploader, b"approve-me")

        reviewed = await review_file(
            db, f.id, FileStatus.APPROVED, review_comment="ok", is_admin=True
        )

        assert reviewed.status == "approved"
        assert reviewed.review_comment == "ok"

    async def should_reject_review_removing_physical(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await self._upload_raw(db, uploader, b"reject-me")

        reviewed = await review_file(
            db, f.id, FileStatus.REJECTED, review_comment="copyright", is_admin=True
        )

        assert reviewed.status == "rejected"
        physical = await self._list_physical(tmp_path)
        assert physical == []

    async def should_reject_review_when_not_pending(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await _au(auth_db)
        f = await self._upload_raw(db, uploader, b"already-done")
        await review_file(db, f.id, FileStatus.APPROVED, is_admin=True)

        with pytest.raises(BizError) as exc:
            await review_file(db, f.id, FileStatus.REJECTED, is_admin=True)
        assert exc.value.errcode == FileErr.NOT_PENDING


class TestContentPath:
    def should_bucket_by_hash_prefix(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ):
        """落盘路径应一层分桶：files_store_dir/<hash[:2]>/<hash>。"""
        from app.modules.files.service import _content_path

        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        h = "ab" + "c" * 62
        p = _content_path(h)
        assert p == tmp_path / "ab" / h
        assert p.parent == tmp_path / "ab"


# ---- Phase 2-A: download/preview ----


def test_file_err_not_approved_maps_403():
    from app.modules.files.errors import FileErr

    assert FileErr.NOT_APPROVED.value is not None  # 存在性


def test_download_url_info_fields():
    m = DownloadUrlInfo(kind="backend", url="/api/v1/files/1/content")
    assert (
        m.kind == "backend"
        and m.url == "/api/v1/files/1/content"
        and m.expires_in is None
    )


# ---- Phase 2-A endpoints ----


class TestFilesPhase2AEndpoints:
    """预览 / 下载 URL / 内容流 三个新端点。

    用 service 层 create_file + review_file(APPROVED) 造出"真实已存盘且已审核通过"的文件，
    使 content backend 能真正读到字节。权限点 files.download 落业务 db，登录身份在 auth realm。
    """

    CONTENT = b"%PDF-1.4 phase2a content"

    async def _approved_file(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        user_id: uuid.UUID,
        approved: bool = True,
    ) -> uuid.UUID:
        f = await create_file(
            db,
            user_id,
            FileCreate(
                original_name="讲义.pdf",
                mime_type="application/pdf",
                category_id="math",
                description="",
                tags=[],
            ),
            stream=io.BytesIO(self.CONTENT),
        )
        if approved:
            await review_file(db, f.id, FileStatus.APPROVED, is_admin=True)
        return f.id

    async def _authed(self, db: AsyncSession, auth_db: AsyncSession) -> AuthUser:
        # 预览/下载端点需 files.download 权限点（test_columns 迁移同款做法）。
        await _grant(db, "normal:member", "files.download")
        return await _au(
            auth_db, username="phase2a", nickname="phase2a", role="member"
        )

    async def test_preview_returns_403_for_non_approved(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=False)

        resp = await client.get(
            f"/api/v1/files/{fid}/preview",
            headers=_h(uploader),
        )

        assert resp.status_code == 403
        assert resp.json()["code"] == FileErr.NOT_APPROVED

    async def test_preview_returns_content_for_approved(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        resp = await client.get(
            f"/api/v1/files/{fid}/preview",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.content == self.CONTENT

    async def test_file_content_is_not_publicly_cached(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        resp = await client.get(
            f"/api/v1/files/{fid}/content",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, no-store"

    async def test_preview_not_publicly_cached(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        resp = await client.get(
            f"/api/v1/files/{fid}/preview",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, no-store"

    async def test_download_url_local_returns_backend(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        assert settings.storage_backend == "local"  # 本测试依赖 local 后端
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        resp = await client.get(
            f"/api/v1/files/{fid}/download/url",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["kind"] == "backend"
        assert body["url"] == f"/api/v1/files/{fid}/content"
        assert body["expires_in"] is None

    async def test_download_url_403_for_non_approved(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=False)

        resp = await client.get(
            f"/api/v1/files/{fid}/download/url",
            headers=_h(uploader),
        )

        assert resp.status_code == 403

    async def test_download_bumps_download_count(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        await client.get(
            f"/api/v1/files/{fid}/download/url",
            headers=_h(uploader),
        )

        row = (
            (await db.execute(select(LibraryFile).where(LibraryFile.id == fid)))
            .scalars()
            .one()
        )
        assert row.download_count == 1

    async def test_content_streams_attachment(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        resp = await client.get(
            f"/api/v1/files/{fid}/content",
            headers=_h(uploader),
        )

        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.content == self.CONTENT

    async def test_preview_bumps_view_count(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)
        fid = await self._approved_file(db, auth_db, uploader.id, approved=True)

        await client.get(
            f"/api/v1/files/{fid}/preview",
            headers=_h(uploader),
        )

        row = (
            (await db.execute(select(LibraryFile).where(LibraryFile.id == fid)))
            .scalars()
            .one()
        )
        assert row.view_count == 1


# ---- Phase 2-B: upload-init / confirm（预签名直传） ----


class _FakeRedis:
    """极简 dict 版 Redis，仅覆盖 upload-init/confirm 用到的 set/getdel。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._data[key] = value

    async def getdel(self, key: str) -> str | None:
        return self._data.pop(key, None)


def _moto_s3_storage():
    """起 moto 内存 S3 + 返回 (S3Storage, moto_client)。mock_aws 上下文须在调用方存活。"""
    import boto3
    from botocore.exceptions import ClientError  # noqa: F401
    from moto import mock_aws  # noqa: F401

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="lkm")
    return S3Storage(bucket="lkm", prefix="files", client=client), client


class TestFilesPhase2BUploadInit:
    """L-b 契约：Local→sync，S3→direct（presigned_url + upload_id）。"""

    async def _authed(self, db: AsyncSession, auth_db: AsyncSession) -> AuthUser:
        # upload-init/confirm 需 files.upload 权限点（test_columns 迁移同款做法）。
        await _grant(db, "normal:member", "files.upload")
        return await _au(
            auth_db,
            username="p2b",
            nickname="p2b",
            account_level="normal",
            role="member",
        )

    def _body(self) -> dict[str, object]:
        return {
            "original_name": "讲座.pdf",
            "mime_type": "application/pdf",
            "category_id": "math",
            "description": "直传测试",
            "tags": ["数学"],
        }

    async def test_upload_init_local_returns_sync(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "storage_backend", "local")
        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        uploader = await self._authed(db, auth_db)

        resp = await client.post(
            "/api/v1/files/upload-init",
            headers=_h(uploader),
            json=self._body(),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["mode"] == "sync"
        assert data["upload_id"] is None
        assert data["presigned_url"] is None

    async def test_upload_init_s3_returns_direct(
        self,
        client: Any,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from moto import mock_aws

        import app.modules.files.service as svc

        monkeypatch.setattr(settings, "files_store_dir", str(tmp_path))
        monkeypatch.setattr(settings, "storage_backend", "s3")
        with mock_aws():
            stor, _client = _moto_s3_storage()
            monkeypatch.setattr(svc, "_get_storage", lambda: stor)
            # S3 直传需 Redis 标记；未注入时 get_redis 返回 None 也对（标记可选）
            uploader = await self._authed(db, auth_db)

            resp = await client.post(
                "/api/v1/files/upload-init",
                headers=_h(uploader),
                json=self._body(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["mode"] == "direct"
        assert data["upload_id"]
        assert data["presigned_url"].startswith("https")

    async def test_upload_init_marker_includes_uploader_id(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S3 直传初始化写下的 Redis 标记必须含 uploader_id（Phase 2-C 事件回调无用户上下文，
        登记归属靠标记携带的 uploader_id）。"""
        from moto import mock_aws

        import app.modules.files.service as svc

        monkeypatch.setattr(settings, "storage_backend", "s3")
        with mock_aws():
            stor, _client = _moto_s3_storage()
            monkeypatch.setattr(svc, "_get_storage", lambda: stor)
            fake = _FakeRedis()

            async def _fake_redis() -> object:
                return fake

            monkeypatch.setattr(svc, "get_redis", _fake_redis)
            uploader = await _au(auth_db)
            from app.modules.files.schemas import FileCreate
            from auth.deps import CurrentUser

            cur = CurrentUser(id=uploader.id, account_level="normal", role="member")
            init = await upload_init(
                db,
                FileCreate(
                    original_name="讲座.pdf",
                    mime_type="application/pdf",
                    category_id="math",
                    description="直传测试",
                    tags=["数学"],
                ),
                cur,
            )

            assert init.mode == "direct"
            assert init.upload_id is not None
            meta_raw = fake._data[svc._upload_key(init.upload_id)]
            meta = json.loads(meta_raw)
            assert meta["uploader_id"] == str(uploader.id)

    async def test_confirm_missing_marker_raises_expired(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redis 标记缺失（或 Redis 未启用）→ UPLOAD_EXPIRED。"""
        from moto import mock_aws

        monkeypatch.setattr(settings, "storage_backend", "s3")
        with mock_aws():
            stor, _ = _moto_s3_storage()
            monkeypatch.setattr(
                "app.modules.files.service._get_storage", lambda: stor
            )
            uploader = await _au(auth_db)
            from auth.deps import CurrentUser

            cur = CurrentUser(id=uploader.id, account_level="normal", role="member")

            with pytest.raises(BizError) as exc:
                await confirm_upload(db, "no-such-upload", cur)

        assert exc.value.errcode == FileErr.UPLOAD_EXPIRED

    async def test_confirm_s3_registers_pending_and_dedups(
        self,
        db: AsyncSession,
        auth_db: AsyncSession,
        auth_seam_realm: None,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """完整 S3 直传确认：读随机 key→SHA3→copy 到内容寻址→登记 PENDING。

        用假 Redis 提供 getdel 标记，moto 提供 put/copy/exists 对象层。
        confirm_upload 尾经 _uploader_map 跨 realm 解析展示名 → 须 seam ON。
        """
        from botocore.exceptions import ClientError
        from moto import mock_aws

        import app.modules.files.service as svc

        monkeypatch.setattr(settings, "storage_backend", "s3")
        with mock_aws():
            stor, client = _moto_s3_storage()
            monkeypatch.setattr(svc, "_get_storage", lambda: stor)
            fake = _FakeRedis()

            async def _fake_redis() -> object:
                return fake

            monkeypatch.setattr(svc, "get_redis", _fake_redis)
            uploader = await _au(auth_db)
            from app.modules.files.schemas import FileCreate
            from auth.deps import CurrentUser

            cur = CurrentUser(id=uploader.id, account_level="normal", role="member")
            init = await upload_init(
                db,
                FileCreate(
                    original_name="讲座.pdf",
                    mime_type="application/pdf",
                    category_id="math",
                    description="直传测试",
                    tags=["数学"],
                ),
                cur,
            )

            assert init.mode == "direct"
            upload_id = init.upload_id
            assert upload_id is not None
            content = b"%PDF-1.4 direct upload bytes"
            client.put_object(Bucket="lkm", Key=f"files/up/{upload_id}", Body=content)

            result = await confirm_upload(db, upload_id, cur)

            assert result.status == "pending"
            assert result.original_name == "讲座.pdf"
            assert result.size == len(content)
            assert result.tags == ["数学"]
            expected = hashlib.sha3_256(content).hexdigest()
            assert len(expected) == 64
            key = f"files/{expected[:2]}/{expected}"
            copied = client.get_object(Bucket="lkm", Key=key)["Body"].read()
            assert copied == content
            with pytest.raises(ClientError):
                client.head_object(Bucket="lkm", Key=f"files/up/{upload_id}")
            # DB 登记 PENDING，ref_count=1
            rows = (await db.execute(select(LibraryFile))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.status == "pending"
            assert row.sha3_hash == expected
            assert row.ref_count == 1
            assert row.storage_path == f"files/{expected[:2]}/{expected}"
