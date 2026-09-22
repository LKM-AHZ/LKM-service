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
    """按配置返回缓存的后端单例（进程内复用，避免重复建 boto3 客户端/连接）。

    取值先归一（去空白 + 小写），且**未知后端显式报错**：原先「不等于 s3 就当 local」会把
    大小写笔误（S3）、多余空白或随便写的 minio 静默降级为本地文件系统——上传落到了容器本地
    盘而无人察觉（settings.storage_backend 是无白名单校验的裸 str）。
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
        )
    raise ValueError(
        f"未知的 LKM_STORAGE_BACKEND={settings.storage_backend!r}（仅支持 local/s3）"
    )
