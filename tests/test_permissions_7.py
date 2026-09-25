"""模块7 错误码注册收敛（require_owner_or_admin 已随 RBAC 迁移删除）。"""

from app.core.err import ERRTABLE, CommonErr, ErrCode
from app.modules.content.columns.errors import ColumnErr
from app.modules.content.errors import ContentErr
from app.modules.files.errors import FileErr


def test_all_error_modules_register_without_duplicate() -> None:
    """导入全部 errors 模块（副作用注册）不抛重复错误码；关键码均已入表。"""
    # 触发全部注册（main 顶部集中 import 亦依赖此机制）
    import app.modules.content.articles.errors
    import app.modules.content.blog.errors
    import app.modules.content.errors
    import app.modules.files.errors
    import app.modules.starhope.errors  # noqa: F401

    # 抽样验证几个模块错误码确实已注册（否则 map_err 会 KeyError 转 500）
    samples: list[ErrCode] = [
        ContentErr.CONTENT_NOT_FOUND,
        FileErr.NOT_FOUND,
        ColumnErr.NOT_FOUND,
        CommonErr.INTERNAL_ERROR,
    ]
    for code in samples:
        assert code in ERRTABLE, f"{code} 未注册"
