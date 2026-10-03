"""文件库安全、编号、检索文档与副本的无数据库回归。"""

from __future__ import annotations

import io
import shutil
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.files.errors import FileErr
from app.modules.files.models import FileClassification, FileStatus
from app.modules.files.processing import process_upload
from app.modules.files.repository import LibraryFileRepository
from app.modules.files.retention import archive_file, backup_file, delete_archive
from app.modules.files.service import _require_visible
from app.modules.search.documents import build_file_doc
from core.config import settings
from core.contracts import CurrentUser
from core.err import BizError


class _Storage:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def open(self, _key: str):
        yield self.payload


def _row(*, status: str = FileStatus.APPROVED, classification: str = "public"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        uploader_id=uuid.uuid4(),
        original_name="论文.pdf",
        stored_name="stored",
        sha3_hash="a" * 64,
        mime_type="application/pdf",
        size=10,
        category_id="math",
        description="量子力学",
        tags='["科学"]',
        status=status,
        classification=classification,
        document_code="WL-SYBG-2024-001",
        version=2,
        extracted_text="量子模型",
        created_at=datetime.now(UTC),
    )


def test_sensitive_terms_reject_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_clamav_address", "")
    monkeypatch.setattr(settings, "files_sensitive_terms", "机密资料")
    with pytest.raises(BizError) as exc:
        process_upload(
            io.BytesIO("此处包含机密资料".encode()),
            original_name="note.txt",
            description="说明",
            size=100,
        )
    assert exc.value.errcode == FileErr.UNSAFE_CONTENT


def test_scanner_failure_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_clamav_address", "127.0.0.1:1")
    monkeypatch.setattr(settings, "files_sensitive_terms", "")
    with pytest.raises(BizError) as exc:
        process_upload(
            io.BytesIO(b"safe"), original_name="a.bin", description="", size=4
        )
    assert exc.value.errcode == FileErr.SCAN_UNAVAILABLE


def test_clamav_found_response_rejects_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.files.processing as processing

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def settimeout(self, _seconds: int) -> None:
            return None

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, _size: int) -> bytes:
            return b"stream: Eicar-Test-Signature FOUND\0"

    fake = FakeSocket()
    monkeypatch.setattr(settings, "files_clamav_address", "scanner:3310")
    monkeypatch.setattr(processing.socket, "create_connection", lambda *_args, **_kwargs: fake)
    with pytest.raises(BizError) as exc:
        process_upload(io.BytesIO(b"payload"), original_name="a.bin", description="", size=7)
    assert exc.value.errcode == FileErr.UNSAFE_CONTENT
    assert fake.sent[0] == b"zINSTREAM\0"


def test_word_document_generates_pdf_and_search_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("libreoffice") is None or shutil.which("pdftotext") is None:
        pytest.skip("Office conversion tools are not installed")
    monkeypatch.setattr(settings, "files_clamav_address", "")
    monkeypatch.setattr(settings, "files_sensitive_terms", "")
    document = tmp_path / "source.docx"
    with zipfile.ZipFile(document, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        package.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Quantum Word Preview</w:t></w:r></w:p>'
            "<w:sectPr/></w:body></w:document>",
        )
    raw = document.read_bytes()
    text, preview = process_upload(
        io.BytesIO(raw), original_name="report.docx", description="", size=len(raw)
    )
    assert preview is not None and preview.startswith(b"%PDF")
    assert "Quantum Word Preview" in text


@pytest.mark.asyncio
async def test_document_codes_increase_inside_year_lock() -> None:
    db = AsyncMock()
    db.scalar.return_value = 7
    repo = LibraryFileRepository(db)
    repo.lock_document = AsyncMock()  # type: ignore[method-assign]
    assert await repo.next_document_code(2024) == "WL-SYBG-2024-008"
    repo.lock_document.assert_awaited_once_with("WL-SYBG-2024-")


@pytest.mark.asyncio
async def test_confidential_file_requires_owner_or_project_member() -> None:
    row = _row(classification=FileClassification.CONFIDENTIAL)
    row.project_id = uuid.uuid4()
    viewer = CurrentUser(id=uuid.uuid4(), account_level="normal", role="member")
    db = AsyncMock()
    db.scalar.return_value = None
    with pytest.raises(BizError) as exc:
        await _require_visible(db, row, viewer)
    assert exc.value.errcode == FileErr.NOT_FOUND
    db.scalar.return_value = uuid.uuid4()
    await _require_visible(db, row, viewer)
    row.status = FileStatus.PENDING
    with pytest.raises(BizError):
        await _require_visible(db, row, viewer)


@pytest.mark.asyncio
async def test_public_preview_visibility_allows_anonymous_user() -> None:
    db = AsyncMock()
    await _require_visible(db, _row(), None)
    with pytest.raises(BizError) as exc:
        await _require_visible(db, _row(classification="internal"), None)
    assert exc.value.errcode == FileErr.NOT_FOUND


def test_public_search_document_contains_extracted_text() -> None:
    doc = build_file_doc(_row(), "上传者")
    assert doc["content_type"] == "library_file"
    assert doc["summary"] == "WL-SYBG-2024-001"
    assert doc["content"] == "量子模型"


@pytest.mark.asyncio
async def test_backup_and_archive_verify_sha3(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    data = b"immutable document bytes"
    digest = hashlib.sha3_256(data).hexdigest()
    monkeypatch.setattr(settings, "files_backup_dir", str(tmp_path / "backup"))
    monkeypatch.setattr(settings, "files_archive_dir", str(tmp_path / "archive"))
    storage = _Storage(data)
    await backup_file(storage, "source", digest)  # type: ignore[arg-type]
    await archive_file(storage, "source", digest)  # type: ignore[arg-type]
    assert (tmp_path / "backup" / digest[:2] / digest).read_bytes() == data
    assert (tmp_path / "archive" / digest[:2] / digest).read_bytes() == data
    await delete_archive(digest)
    assert not (tmp_path / "archive" / digest[:2] / digest).exists()
    assert (tmp_path / "backup" / digest[:2] / digest).exists()
    with pytest.raises(BizError):
        await backup_file(storage, "bad", "0" * 64)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_download_stream_reads_cold_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import app.modules.files.service as service

    data = b"archived bytes"
    digest = hashlib.sha3_256(data).hexdigest()
    archive = tmp_path / digest[:2] / digest
    archive.parent.mkdir(parents=True)
    archive.write_bytes(data)
    monkeypatch.setattr(settings, "files_archive_dir", str(tmp_path))
    row = _row()
    row.sha3_hash = digest
    row.original_name = "report.pdf"

    class MissingStorage:
        async def exists(self, _key: str) -> bool:
            return False

    async def get_row(*_args, **_kwargs):
        return row

    monkeypatch.setattr(service, "get_or_raise", get_row)
    monkeypatch.setattr(service, "_get_storage", lambda: MissingStorage())
    response = await service.serve_content(AsyncMock(), row.id, "attachment")
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == data


@pytest.mark.asyncio
async def test_archived_s3_file_uses_backend_download_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import app.modules.files.service as service

    data = b"archived s3 bytes"
    digest = hashlib.sha3_256(data).hexdigest()
    archive = tmp_path / digest[:2] / digest
    archive.parent.mkdir(parents=True)
    archive.write_bytes(data)
    monkeypatch.setattr(settings, "files_archive_dir", str(tmp_path))
    monkeypatch.setattr(settings, "storage_backend", "s3")
    row = _row()
    row.sha3_hash = digest
    user = CurrentUser(id=row.uploader_id, account_level="normal", role="member")

    async def get_row(*_args, **_kwargs):
        return row

    class FakeRepo:
        def __init__(self, _db: object) -> None:
            pass

        async def increment_download(self, _file_id: uuid.UUID) -> int:
            return 1

    monkeypatch.setattr(service, "get_or_raise", get_row)
    monkeypatch.setattr(service, "LibraryFileRepository", FakeRepo)
    monkeypatch.setattr(service, "_get_storage", lambda: object())
    result = await service.download_url(AsyncMock(), row.id, user)
    assert result.kind == "backend"
    assert result.url == f"/api/v1/files/{row.id}/content"
