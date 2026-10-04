"""
存储后端抽象接口：``StorageBackend`` 协议 + ``SavedFile`` 记录。
``StorageBackend`` 是 Local/S3 两个后端共同遵循的最小协议（save/open/delete/exists +
预签名），files 层依赖该协议而非具体后端实现。
"""

from collections.abc import AsyncIterator
from typing import Protocol, TypedDict


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class SavedFile(TypedDict):
    """
    ``save()`` 的返回记录：三个键**都必填**。
    原先声明成 ``total=False``（全可选）与两个后端和调用方的实际契约不符——Local/S3 都
    会填满三个键，而 files 层直接下标取值（如 ``str(saved["storage_path"])``）；写成可选
    后，漏键的后端在类型层面看不出来，只会在运行期以 KeyError 炸出来。
    """

    size: int
    bucket_key: str
    storage_path: str


class StorageBackend(Protocol):
    async def save(
        self, stream: Readable, /, *, max_bytes: int, bucket_key: str
    ) -> SavedFile: ...

    def open(self, bucket_key: str) -> AsyncIterator[bytes]: ...

    async def copy(self, src: str, dest: str) -> None: ...

    async def delete(self, bucket_key: str) -> None: ...

    async def exists(self, bucket_key: str) -> bool: ...

    def presign_download(self, bucket_key: str, *, expires: int) -> str: ...

    def presign_upload(self, bucket_key: str, *, expires: int) -> str: ...
