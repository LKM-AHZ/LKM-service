# app/modules/auth/errors.py  (M3 peer-package; 真定义已在 app.core.err)
"""auth 旧专用错误已并入共享 app.core.err；本文件仅为 auth 内部旧路径保留私有重导出。"""
from app.core.err import (
    AuthErr,  # noqa: F401  (仅 auth 内部; 外部(throttle/session)已改走共享)
)
