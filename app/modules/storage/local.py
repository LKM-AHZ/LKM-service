"""本地磁盘存储后端：把 ``bucket_key`` 映射到 ``root_dir`` 下的路径。

字节级存取，不负责内容寻址/去重策略（由 files 层决定 key 形状、计算哈希）。落盘
逻辑收编自 ``app/modules/files/service.py`` 的 ``_write_upload``/``_stream_to_disk_hash``
/``_new_temp_file``，仅改变路径来源改写而保留其安全语义。
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

from app.core.err import BizError
from app.modules.storage.base import SavedFile
from app.modules.storage.errors import StorageErr

_CHUNK = 1024 * 1024  # 分块读写，避免整文件载入内存


class _Readable(Protocol):
    """可同步分块读取的 file-like 对象最小协议。"""

    def read(self, size: int = -1, /) -> bytes: ...


class LocalStorage:
    """实现 :class:`StorageBackend` 的本地磁盘后端。

    ``bucket_key`` 是 files 层传入的**裸逻辑 key**（形如 ``ab/<hash>``，不带 ``files/``
    前缀），映射到 ``root_dir / bucket_key``；为防路径穿越，拒绝含 ``..`` 段或绝对路径的
    key。所有阻塞磁盘 I/O 经 ``asyncio.to_thread``。
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def _resolve(self, bucket_key: str) -> Path:
        """把 bucket_key 规范化为 root 下安全绝对路径，含路径穿越防护。

        拒绝空 key、绝对路径、含 ``..`` 段或含盘符（``C:``/stream 等）逃逸的 key；
        解析后仍强制校验落点在 root 下，防御平台特定的无法枚举的穿越形式。
        """
        if not bucket_key:
            raise BizError(StorageErr.STORE_ERROR, detail="Invalid storage key")
        if (
            bucket_key.startswith("/")
            or bucket_key.startswith("\\")
            or ":" in bucket_key.split("/", 1)[0]
            or ":" in bucket_key.split("\\", 1)[0]
        ):
            raise BizError(StorageErr.STORE_ERROR, detail="Invalid storage key")
        parts = Path(bucket_key).parts
        if ".." in parts:
            raise BizError(StorageErr.STORE_ERROR, detail="Invalid storage key")
        root = self.root_dir.resolve()
        dest = (root / bucket_key).resolve()
        # 还要挡住「归一化后正好等于 root」的 key（"." / "./" 等）：Path(".").parts 为空、
        # 不含 ".."，能过前面的检查，落到这里就会被当成文件路径（delete 变 IsADirectoryError、
        # exists 恒真、save 试图 temp.replace(root)）
        if dest == root or not dest.is_relative_to(root):
            raise BizError(StorageErr.STORE_ERROR, detail="Invalid storage key")
        return dest

    async def save(
        self, stream: Any, /, *, max_bytes: int, bucket_key: str
    ) -> SavedFile:
        dest = self._resolve(bucket_key)
        # 临时文件在 try 之外就可能创建失败，故先置空、回收时判空
        temp: Path | None = None
        try:
            # 落盘前先建好 root：全新部署/本地首跑时 store 目录不存在，NamedTemporaryFile
            # 会直接 FileNotFoundError，导致每次上传都以 STORE_ERROR 失败。
            # 这一步也必须在 try 内：root 的父路径被普通文件占住时 mkdir 抛的是
            # NotADirectoryError（OSError 子类），漏到外面会变成 500 而不是业务错误，
            # 上层也就无法据此回滚已落库的记录。
            await asyncio.to_thread(self.root_dir.mkdir, parents=True, exist_ok=True)
            temp = await asyncio.to_thread(_new_temp_file, self.root_dir)
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            size, _hash = await asyncio.to_thread(
                _stream_to_disk_hash, stream, temp, max_bytes
            )
            await asyncio.to_thread(temp.replace, dest)
        except BizError:
            if temp is not None:
                await asyncio.to_thread(_safe_unlink, temp)
            raise
        except OSError as exc:
            if temp is not None:
                await asyncio.to_thread(_safe_unlink, temp)
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to store file: {exc}"
            ) from exc
        except BaseException:
            # 非 BizError/OSError 的异常（stream.read 抛的自定义异常、请求被取消等）
            # 同样要回收临时文件，否则 .tmp 会一直留在 store 根目录
            if temp is not None:
                await asyncio.to_thread(_safe_unlink, temp)
            raise
        return {
            "size": size,
            "bucket_key": bucket_key,
            "storage_path": str(dest),
        }

    async def open(self, bucket_key: str) -> AsyncIterator[bytes]:
        """按块读出对象内容。

        一次性打开文件描述符后连续读：原实现每块按路径重新 open+seek，mid-stream 若被
        并发 delete/replace 换掉，会读到另一个 inode 的错位数据、或在已吐出部分内容后
        抛 NOT_FOUND（响应被截断）。持有 fd 则拿到稳定快照（文件被 unlink 仍可读完）。
        """
        dest = self._resolve(bucket_key)
        handle = await asyncio.to_thread(_open_reader, dest)
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, _CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            with suppress(OSError):
                await asyncio.to_thread(handle.close)

    async def copy(self, src: str, dest: str) -> None:
        raise NotImplementedError("Local backend 无 confirm/副本流程")

    async def delete(self, bucket_key: str) -> None:
        dest = self._resolve(bucket_key)
        await asyncio.to_thread(dest.unlink, missing_ok=True)

    async def exists(self, bucket_key: str) -> bool:
        dest = self._resolve(bucket_key)
        return await asyncio.to_thread(dest.exists)

    def presign_download(self, bucket_key: str, *, expires: int) -> str:
        raise NotImplementedError("Local backend 无签名 URL")

    def presign_upload(self, bucket_key: str, *, expires: int) -> str:
        raise NotImplementedError("Local backend 无签名 URL")


def _safe_unlink(path: Path) -> None:
    """清理临时文件：失败不抛（否则会覆盖原始异常，掩盖真正的失败原因）。"""
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _new_temp_file(root_dir: Path) -> Path:
    """在 root 下建一个安全命名的临时文件，避免原文件名中的路径穿越。

    需 ``delete=False`` 并在拿到 ``.name`` 后立即关闭，交由调用方决定 move/删除，
    不能走 context manager（会提前删文件）。root 不可写时抛 ``STORE_ERROR``。
    """
    try:
        tmp = NamedTemporaryFile(  # noqa: SIM115
            dir=root_dir, suffix=".tmp", delete=False
        )
    except OSError as exc:
        raise BizError(
            StorageErr.STORE_ERROR, detail=f"Failed to store file: {exc}"
        ) from exc
    path = Path(tmp.name)
    tmp.close()
    return path


def _stream_to_disk_hash(
    stream: _Readable, dest_path: Path, limit: int
) -> tuple[int, str]:
    """同步分块读取 ``stream`` 写盘，返回 ``(总字节数, SHA3-256 hex)``。

    在 async 端点内通过 asyncio.to_thread 调度，避免文件读写（含建目录）阻塞事件循环。
    超过 ``limit`` 抛 ``TOO_LARGE``；OS 错误抛 ``STORE_ERROR``。
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha3_256()
    total = 0
    with dest_path.open("wb") as out:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise BizError(
                    StorageErr.TOO_LARGE,
                    detail=f"Upload exceeds {limit} byte limit",
                )
            hasher.update(chunk)
            out.write(chunk)
    return total, hasher.hexdigest()


def _open_reader(dest_path: Path) -> Any:
    """打开待读文件；不存在 → NOT_FOUND，其余 OS 错误 → STORE_ERROR。

    原先把所有 OSError 都归为 NOT_FOUND，权限/目录/磁盘故障都会被报成 404。
    """
    try:
        return dest_path.open("rb")
    except FileNotFoundError as exc:
        raise BizError(
            StorageErr.NOT_FOUND, detail=f"Storage key not found: {exc}"
        ) from exc
    except OSError as exc:
        raise BizError(
            StorageErr.STORE_ERROR, detail=f"Failed to read: {exc}"
        ) from exc
