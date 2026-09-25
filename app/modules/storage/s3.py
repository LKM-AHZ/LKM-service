"""S3/MinIO 存储后端：把 ``bucket_key`` 存到 ``prefix`` 下的真实 S3 key。

与 :class:`~app.modules.storage.local.LocalStorage` 一样字节级、不负责内容寻址/去重
（由 files 层决定 key 形状）。所有网络 I/O 经 ``asyncio.to_thread`` 调度，避免阻塞事件循环。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.err import BizError
from app.modules.storage.base import SavedFile
from app.modules.storage.errors import StorageErr

_CHUNK = 1024 * 1024  # 下载读取分块
# multipart 除末块外每块必须 >= 5 MiB（S3/MinIO 硬约束）：沿用 1 MiB 分块上传时，
# 任何 >1 MiB 的对象都会在 complete_multipart_upload 报 EntityTooSmall
_PART_SIZE = 5 * 1024 * 1024


class _TooLarge(Exception):
    """内部标记：累计超限。在 ``save`` 层转换为 ``StorageErr.TOO_LARGE``。"""


def _save_multipart_sync(
    client: Any, bucket: str, key: str, stream: Any, max_bytes: int
) -> int:
    """同步分块写入 S3（multipart）：create -> upload_part*n -> complete。

    ``stream`` 需提供 ``read(size=-1)``（对 files 层传入的 ``io.BytesIO`` 兼容）。边读边累计，
    超 ``max_bytes`` 立即 abort 并向调用方抛内部标记（转 TOO_LARGE），不留残留。
    """
    resp = client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = resp["UploadId"]
    parts: list[dict[str, str | int]] = []
    size = 0
    try:
        while True:
            chunk = stream.read(_PART_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise _TooLarge()
            part = client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=len(parts) + 1,
                Body=chunk,
            )
            parts.append({"PartNumber": len(parts) + 1, "ETag": part["ETag"]})
        if not parts:
            # 0 字节对象：multipart 不允许 0 个 part（会走到这里），但空文件本身合法，
            # 改用 put_object 落空对象，不能当成「超限」报 413。已创建的 multipart
            # 须先 abort，否则返回路径绕过了下面的 except，留下孤儿分片。
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            client.put_object(Bucket=bucket, Key=key, Body=b"")
            return 0
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return size
    except BaseException:
        # 出错（含超限）时中止未完成的 multipart，避免孤儿分片（中止失败不影响原异常抛出）
        with suppress(Exception):
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


class S3Storage:
    """实现 :class:`StorageBackend` 的 S3/MinIO 后端。

    ``bucket_key`` 是 files 层传入的**裸逻辑 key**（形如 ``ab/<hash>``，不带 ``files/``
    前缀）；真实 S3 key = ``f"{prefix}/{bucket_key.lstrip('/')}"``（prefix 常为 ``files``）。
    构造入参里 ``client`` 可注入 mock（moto 测试用），否则按连接参数自建。
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        client: Any = None,
        endpoint_url: str = "",
        public_endpoint_url: str = "",
        region_name: str = "",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        addressing_style: str = "path",
        public_addressing_style: str = "path",
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        # 客户端构造为同步一次性调用，在工厂/注入处完成，常驻；网络回调走 to_thread
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=aws_access_key_id or None,
            aws_secret_access_key=aws_secret_access_key or None,
            # 寻址风格可配（蓝图 §6.3）：OSS/COS 需 virtual-host，S3/MinIO 用 path。
            config=Config(s3={"addressing_style": addressing_style}),
        )
        # 预签名 URL 对浏览器暴露的公网 endpoint。签名与请求 host 必须一致，故用
        # 独立 client（endpoint=公网）生成 preset 签名，否则 host 与签名不符会 403。
        # 空则复用内网 client（浏览器可直连时才适用）。
        self._public_client = self._client
        if public_endpoint_url:
            # MinIO 校验预签名用 SigV4；boto3 对非 AWS 标准 endpoint 默认 SigV2 会 403。
            # 需显式 s3v4 + path 寻址，并给 region（SigV4 要求），公网 host 供浏览器直连。
            self._public_client = boto3.client(
                "s3",
                endpoint_url=public_endpoint_url,
                region_name=region_name or "us-east-1",
                aws_access_key_id=aws_access_key_id or None,
                aws_secret_access_key=aws_secret_access_key or None,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": public_addressing_style},
                ),
            )

    def _key(self, bucket_key: str) -> str:
        # bucket_key 是 files 层传下来的**裸逻辑 key**（_build_bucket_key 产出 <hash[:2]>/<hash>，
        # 不含 prefix）：prefix 正是在这里拼上去的；若误以为它已带 prefix 再拼一次会得到 files/files/...
        if not self.prefix:
            return bucket_key
        return f"{self.prefix}/{bucket_key.lstrip('/')}"

    async def save(
        self, stream: Any, /, *, max_bytes: int, bucket_key: str
    ) -> SavedFile:
        key = self._key(bucket_key)
        # multipart 全程为同步 I/O（每个 part 一次调用），交给线程池执行，避免阻塞事件循环。
        try:
            size = await asyncio.to_thread(
                _save_multipart_sync, self._client, self.bucket, key, stream, max_bytes
            )
        except _TooLarge as exc:
            raise BizError(StorageErr.TOO_LARGE) from exc
        except ClientError as exc:
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to store: {exc}"
            ) from exc
        return {"size": size, "bucket_key": bucket_key, "storage_path": key}

    async def open(self, bucket_key: str) -> AsyncIterator[bytes]:
        try:
            resp = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self.bucket,
                Key=self._key(bucket_key),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
                raise BizError(StorageErr.NOT_FOUND) from exc
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to read: {exc}"
            ) from exc
        # 逐块读取经 to_thread 调度，避免 StreamingBody 的同步 socket I/O 阻塞事件循环。
        # body 必须在 finally 关闭：消费方提前 break/抛错时若不关，HTTP 连接与 socket
        # 会一直挂到 GC；流中途的网络错误也要映射成 STORE_ERROR 而不是裸 botocore 异常。
        body = resp["Body"]
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(body.read, _CHUNK)
                except ClientError as exc:
                    raise BizError(
                        StorageErr.STORE_ERROR, detail=f"Failed to read: {exc}"
                    ) from exc
                if not chunk:
                    break
                yield chunk
        finally:
            with suppress(Exception):
                await asyncio.to_thread(body.close)

    async def copy(self, src: str, dest: str) -> None:
        # confirm 流程把随机 key 的对象复制到内容寻址 key；阻塞网络调用走 to_thread
        try:
            await asyncio.to_thread(
                self._client.copy_object,
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": self._key(src)},
                Key=self._key(dest),
            )
        except ClientError as exc:
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to copy: {exc}"
            ) from exc

    async def delete(self, bucket_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self.bucket,
                Key=self._key(bucket_key),
            )
        except ClientError as exc:
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to delete: {exc}"
            ) from exc

    async def exists(self, bucket_key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self.bucket,
                Key=self._key(bucket_key),
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return False
            raise BizError(
                StorageErr.STORE_ERROR, detail=f"Failed to check: {exc}"
            ) from exc

    def presign_download(self, bucket_key: str, *, expires: int) -> str:
        # 预签名 URL 为本地签名计算（无网络），可同步调用。
        # 用公网 client（endpoint=public）生成，保证 URL host 与签名一致。
        return self._public_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(bucket_key)},
            ExpiresIn=expires,
        )

    def presign_upload(self, bucket_key: str, *, expires: int) -> str:
        return self._public_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": self._key(bucket_key)},
            ExpiresIn=expires,
        )
