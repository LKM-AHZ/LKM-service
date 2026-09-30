"""存储后端抽象接口：``StorageBackend`` 协议 + ``SavedFile`` 记录。

``StorageBackend`` 是 Local/S3 两个后端共同遵循的最小协议（save/open/delete/exists +
预签名），files 层依赖该协议而非具体后端实现。
"""

from collections.abc import AsyncIterator
from typing import IO, Protocol, TypedDict


class SavedFile(TypedDict):
    """``save()`` 的返回记录：三个键**都必填**。

    原先声明成 ``total=False``（全可选）与两个后端和调用方的实际契约不符——Local/S3 都
    会填满三个键，而 files 层直接下标取值（如 ``str(saved["storage_path"])``）；写成可选
    后，漏键的后端在类型层面看不出来，只会在运行期以 KeyError 炸出来。
    """

    size: int
    bucket_key: str
    storage_path: str


class StorageBackend(Protocol):
    # 两个后端都按「同步、二进制、可读」使用该流（Local 丢进线程池逐块读、S3 逐 part
    # 同步发送），故显式标注 IO[bytes]——传异步迭代器/文本流会在类型层面即被拦下
    async def save(
        self, stream: IO[bytes], /, *, max_bytes: int, bucket_key: str
    ) -> SavedFile: ...

    # 异步生成器方法：真实后端经 `async def open(...) ... yield` 实现，其可调用类型为
    # Callable 返回 AsyncIterator[bytes]；故协议用普通 def（非 async）标注生成器函数形态，
    # 使 AsyncGenerator <: AsyncIterator 满足结构兼容（async def + body=`...` 会被 ty 当协程返回，不匹配）
    def open(self, bucket_key: str) -> AsyncIterator[bytes]: ...

    # 仅 S3 后端可用的方法（Local 无 confirm/副本流程，调用即 NotImplementedError）。
    # 现存唯一调用点是直传登记（files.service._register_from_upload），而直传流程只在
    # S3 下存在（Local 的 upload-init 返回 mode=sync），故不会落到 Local；
    # 将来新增调用方必须先确认后端为 S3。
    async def copy(self, src: str, dest: str) -> None: ...

    async def delete(self, bucket_key: str) -> None: ...

    async def exists(self, bucket_key: str) -> bool: ...

    def presign_download(self, bucket_key: str, *, expires: int) -> str: ...

    def presign_upload(self, bucket_key: str, *, expires: int) -> str: ...
