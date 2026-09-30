"""存储抽象层：把字节存取从 files 业务解耦，Local/S3 双后端。"""

from core.storage.base import StorageBackend
from core.storage.errors import StorageErr

__all__ = ["StorageBackend", "StorageErr"]
