"""存储后端工厂：按 ``settings.storage_backend`` 返回 Local 或 S3 单例。"""

from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.core.secrets import reveal
from app.modules.storage.base import StorageBackend
from app.modules.storage.local import LocalStorage
from app.modules.storage.s3 import S3Storage


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """按配置返回缓存的后端单例（进程内复用，避免重复建 boto3 客户端/连接）。"""
    if settings.storage_backend == "s3":
        return S3Storage(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            public_endpoint_url=settings.s3_public_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=reveal(settings.s3_access_key),
            aws_secret_access_key=reveal(settings.s3_secret_key),
        )
    return LocalStorage(root_dir=Path(settings.files_store_dir))
