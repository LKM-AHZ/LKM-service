"""存储后端工厂：按 ``settings.storage_backend`` 返回 Local 或 S3 单例。"""

from functools import lru_cache
from pathlib import Path

from core.config import settings
from core.secrets import reveal
from core.storage.base import StorageBackend
from core.storage.local import LocalStorage
from core.storage.s3 import S3Storage


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """按配置返回缓存的后端单例（进程内复用，避免重复建 boto3 客户端/连接）。

    取值先归一（去空白 + 小写），且**未知后端显式报错**：原先「不等于 s3 就当 local」会把
    大小写笔误（S3）、多余空白或随便写的 minio 静默降级为本地文件系统——上传落到了容器本地
    盘而无人察觉（settings.storage_backend 是无白名单校验的裸 str）。

    缓存是刻意的（进程内复用后端与已 reveal 的凭据），代价是运行期改 settings
    （含测试 monkeypatch）不会生效：需要重建时调用 ``get_storage.cache_clear()``。
    """
    backend = (settings.storage_backend or "").strip().lower()
    if backend == "local":
        return LocalStorage(root_dir=Path(settings.files_store_dir))
    if backend == "s3":
        return S3Storage(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            public_endpoint_url=settings.s3_public_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=reveal(settings.s3_access_key),
            aws_secret_access_key=reveal(settings.s3_secret_key),
            addressing_style=settings.s3_addressing_style,
            public_addressing_style=settings.s3_public_addressing_style,
        )
    raise ValueError(
        f"未知的 LKM_STORAGE_BACKEND={settings.storage_backend!r}（仅支持 local/s3）"
    )


_settings_signature: tuple[object, ...] = ()


def get_storage_for_settings() -> StorageBackend:
    """测试修改存储配置时重建工厂缓存；生产配置固定时复用单例。"""
    global _settings_signature
    signature = (
        settings.storage_backend,
        settings.files_store_dir,
        settings.s3_endpoint_url,
        settings.s3_public_endpoint_url,
        settings.s3_region,
        settings.s3_bucket,
        settings.s3_prefix,
        reveal(settings.s3_access_key),
        reveal(settings.s3_secret_key),
        settings.s3_addressing_style,
        settings.s3_public_addressing_style,
    )
    if signature != _settings_signature:
        get_storage.cache_clear()
        _settings_signature = signature
    return get_storage()
