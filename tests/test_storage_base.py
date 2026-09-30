from core.storage import StorageBackend, StorageErr


def test_storage_module_importable():
    # 模块存在、类型可导入（尚未实现时无法 import，任何实现前此测试 FAIL）
    assert StorageBackend is not None
    assert StorageErr.NOT_FOUND is not None
