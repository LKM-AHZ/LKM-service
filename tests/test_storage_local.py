import asyncio
import io
import pathlib

import pytest

from core.err import BizError
from core.storage.errors import StorageErr
from core.storage.local import LocalStorage


@pytest.fixture
def local(tmp_path: pathlib.Path) -> LocalStorage:
    return LocalStorage(root_dir=tmp_path)


def _stream(data: bytes):
    return io.BytesIO(data)


def test_save_writes_under_root_and_returns_size(
    local: LocalStorage, tmp_path: pathlib.Path
):
    key = "files/ab/abc123abc123abc123abc123abc123abc123abc123"
    out = asyncio.run(local.save(_stream(b"hello"), max_bytes=100, bucket_key=key))
    assert out["size"] == 5
    assert out["bucket_key"] == key
    # storage_path 应落在 root 下
    path = pathlib.Path(out["storage_path"])
    assert path.is_relative_to(tmp_path)
    assert path.read_bytes() == b"hello"


def test_save_rejects_over_limit(local: LocalStorage):
    with pytest.raises(BizError) as ei:
        asyncio.run(local.save(_stream(b"x" * 10), max_bytes=5, bucket_key="k"))
    assert ei.value.errcode == StorageErr.TOO_LARGE


def test_save_rejects_path_traversal_dotdot(local: LocalStorage):
    with pytest.raises(BizError):
        asyncio.run(local.save(_stream(b"x"), max_bytes=100, bucket_key="../../escape"))


def test_save_rejects_absolute_path(local: LocalStorage):
    with pytest.raises(BizError):
        asyncio.run(local.save(_stream(b"x"), max_bytes=100, bucket_key="/etc/passwd"))


def test_save_rejects_windows_drive_escape(local: LocalStorage):
    with pytest.raises(BizError):
        asyncio.run(local.save(_stream(b"x"), max_bytes=100, bucket_key="C:/evil"))


def test_exists_and_delete(local: LocalStorage):
    key = "files/ab/cd1234"
    asyncio.run(local.save(_stream(b"data"), max_bytes=100, bucket_key=key))
    asyncio.run(local.save(_stream(b"data"), max_bytes=100, bucket_key=key))
    assert asyncio.run(local.exists(key)) is True
    asyncio.run(local.delete(key))
    assert asyncio.run(local.exists(key)) is False


def test_open_reads_chunked_bytes(local: LocalStorage):
    payload = b"x" * (1024 * 1024 * 2 + 123)
    key = "files/ab/big"
    asyncio.run(local.save(_stream(payload), max_bytes=10**9, bucket_key=key))

    async def collect():
        return b"".join([chunk async for chunk in local.open(key)])

    assert asyncio.run(collect()) == payload


def test_open_missing_raises_not_found(local: LocalStorage):
    async def consume():
        agen = local.open("files/ab/missing")
        await agen.__anext__()

    with pytest.raises(BizError) as ei:
        asyncio.run(consume())
    assert ei.value.errcode == StorageErr.NOT_FOUND


def test_delete_missing_is_noop(local: LocalStorage):
    # 删除不存在的 key 应静默成功（与现有 _write_upload 的 missing_ok 语义一致）
    asyncio.run(local.delete("files/ab/nope"))
